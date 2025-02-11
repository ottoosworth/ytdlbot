#!/usr/local/bin/python3
# coding: utf-8

# ytdlbot - tasks.py
# 12/29/21 14:57
#

__author__ = "Benny <benny.think@gmail.com>"

import logging
import os
import pathlib
import re
import subprocess
import tempfile
import threading
import time
from urllib.parse import quote_plus

import psutil
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from celery import Celery
from celery.worker.control import Panel
from pyrogram import idle
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from client_init import create_app
from config import (ARCHIVE_ID, AUDIO_FORMAT, BROKER, ENABLE_CELERY,
                    ENABLE_VIP, TG_MAX_SIZE, WORKERS)
from constant import BotText
from db import Redis
from downloader import (edit_text, sizeof_fmt, tqdm_progress, upload_hook,
                        ytdl_download)
from limit import VIP
from utils import (apply_log_formatter, auto_restart, customize_logger,
                   get_metadata, get_user_settings)

customize_logger(["pyrogram.client", "pyrogram.session.session", "pyrogram.connection.connection"])
apply_log_formatter()
bot_text = BotText()
logging.getLogger('apscheduler.executors.default').propagate = False

# celery -A tasks worker --loglevel=info --pool=solo
# app = Celery('celery', broker=BROKER, accept_content=['pickle'], task_serializer='pickle')
app = Celery('tasks', broker=BROKER)

celery_client = create_app(":memory:")


def get_messages(chat_id, message_id):
    try:
        return celery_client.get_messages(chat_id, message_id)
    except ConnectionError as e:
        logging.critical("WTH!!! %s", e)
        celery_client.start()
        return celery_client.get_messages(chat_id, message_id)


@app.task()
def ytdl_download_task(chat_id, message_id, url):
    logging.info("YouTube celery tasks started for %s", url)
    bot_msg = get_messages(chat_id, message_id)
    ytdl_normal_download(bot_msg, celery_client, url)
    logging.info("YouTube celery tasks ended.")


@app.task()
def audio_task(chat_id, message_id):
    logging.info("Audio celery tasks started for %s-%s", chat_id, message_id)
    bot_msg = get_messages(chat_id, message_id)
    normal_audio(bot_msg, celery_client)
    logging.info("Audio celery tasks ended.")


def get_unique_clink(clink, settings):
    try:
        unique = "{}?p={}{}".format(clink, *settings[1:])
    except IndexError:
        unique = clink
    return unique


@app.task()
def direct_download_task(chat_id, message_id, url):
    logging.info("Direct download celery tasks started for %s", url)
    bot_msg = get_messages(chat_id, message_id)
    direct_normal_download(bot_msg, celery_client, url)
    logging.info("Direct download celery tasks ended.")


def forward_video(chat_id, url, client):
    red = Redis()
    vip = VIP()
    settings = get_user_settings(str(chat_id))
    clink = vip.extract_canonical_link(url)
    unique = get_unique_clink(clink, settings)

    data = red.get_send_cache(unique)
    if not data:
        return False

    for uid, mid in data.items():
        uid, mid = int(uid), int(mid)
        try:
            result_msg = client.get_messages(uid, mid)
            logging.info("Forwarding message from %s %s %s to %s", clink, uid, mid, chat_id)
            m = result_msg.forward(chat_id)
            red.update_metrics("cache_hit")
            if ENABLE_VIP:
                file_size = getattr(result_msg.document, "file_size", None) or \
                            getattr(result_msg.video, "file_size", 1024)
                # TODO: forward file size may exceed the limit
                vip.use_quota(chat_id, file_size)
            red.add_send_cache(unique, chat_id, m.message_id)
            return True
        except Exception as e:
            logging.error("Failed to forward message %s", e)
            red.del_send_cache(unique, uid)
            red.update_metrics("cache_miss")


def ytdl_download_entrance(bot_msg, client, url):
    chat_id = bot_msg.chat.id
    if forward_video(chat_id, url, client):
        return

    if ENABLE_CELERY:
        ytdl_download_task.delay(chat_id, bot_msg.message_id, url)
    else:
        ytdl_normal_download(bot_msg, client, url)


def direct_download_entrance(bot_msg, client, url):
    if ENABLE_CELERY:
        # TODO disable it for now
        direct_normal_download(bot_msg, client, url)
        # direct_download_task.delay(bot_msg.chat.id, bot_msg.message_id, url)
    else:
        direct_normal_download(bot_msg, client, url)


def audio_entrance(bot_msg, client):
    if ENABLE_CELERY:
        audio_task.delay(bot_msg.chat.id, bot_msg.message_id)
    else:
        normal_audio(bot_msg, client)


def direct_normal_download(bot_msg, client, url):
    chat_id = bot_msg.chat.id
    headers = {
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/80.0.3987.149 Safari/537.36"}
    vip = VIP()
    length = 0
    if ENABLE_VIP:
        remain, _, _ = vip.check_remaining_quota(chat_id)
        try:
            head_req = requests.head(url, headers=headers)
            length = int(head_req.headers.get("content-length"))
        except (TypeError, requests.exceptions.RequestException):
            length = 0
        if remain < length:
            bot_msg.reply_text(f"Sorry, you have reached your quota.\n")
            return

    req = None
    try:
        req = requests.get(url, headers=headers, stream=True)
        length = int(req.headers.get("content-length"))
        filename = re.findall("filename=(.+)", req.headers.get("content-disposition"))[0]
    except TypeError:
        filename = getattr(req, "url", "").rsplit("/")[-1]
    except Exception as e:
        bot_msg.edit_text(f"Download failed!❌\n\n```{e}```", disable_web_page_preview=True)
        return

    if not filename:
        filename = quote_plus(url)

    with tempfile.TemporaryDirectory() as f:
        filepath = f"{f}/{filename}"
        # consume the req.content
        downloaded = 0
        for chunk in req.iter_content(1024 * 1024):
            text = tqdm_progress("Downloading...", length, downloaded)
            edit_text(bot_msg, text)
            with open(filepath, "ab") as fp:
                fp.write(chunk)
            downloaded += len(chunk)
        logging.info("Downloaded file %s", filename)
        st_size = os.stat(filepath).st_size
        if ENABLE_VIP:
            vip.use_quota(chat_id, st_size)
        client.send_chat_action(chat_id, "upload_document")
        client.send_document(bot_msg.chat.id, filepath,
                             caption=f"filesize: {sizeof_fmt(st_size)}",
                             progress=upload_hook, progress_args=(bot_msg,),
                             )
        bot_msg.edit_text(f"Download success!✅")


def normal_audio(bot_msg, client):
    chat_id = bot_msg.chat.id
    fn = getattr(bot_msg.video, "file_name", None) or getattr(bot_msg.document, "file_name", None)
    with tempfile.TemporaryDirectory() as tmp:
        logging.info("downloading to %s", tmp)
        base_path = pathlib.Path(tmp)
        video_path = base_path.joinpath(fn)
        audio = base_path.joinpath(fn).with_suffix(f".{AUDIO_FORMAT}")
        client.send_chat_action(chat_id, 'record_video_note')
        client.download_media(bot_msg, video_path)
        logging.info("downloading complete %s", video_path)
        # execute ffmpeg
        client.send_chat_action(chat_id, 'record_audio')
        try:
            subprocess.check_output(f'ffmpeg -y -i "{video_path}" -vn -acodec copy "{audio}"', shell=True)
        except subprocess.CalledProcessError:
            subprocess.check_output(f"ffmpeg -y -i '{video_path}' '{audio}'", shell=True)

        client.send_chat_action(chat_id, 'upload_audio')
        client.send_audio(chat_id, audio)
        Redis().update_metrics("audio_success")


def get_dl_source():
    worker_name = os.getenv("WORKER_NAME")
    if worker_name:
        return f"Downloaded by  {worker_name}"
    return ""


def upload_transfer_sh(video_paths) -> "str":
    file = {}
    for p in video_paths:
        file[p.name] = p.open("rb")
    try:
        req = requests.post("https://transfer.sh", files=file)
        return req.text
    except requests.exceptions as e:
        return f"Upload failed!❌\n\n```{e}```"


def ytdl_normal_download(bot_msg, client, url):
    chat_id = bot_msg.chat.id
    temp_dir = tempfile.TemporaryDirectory()
    red = Redis()
    result = ytdl_download(url, temp_dir.name, bot_msg)
    logging.info("Download complete.")
    markup = InlineKeyboardMarkup(
        [
            [  # First row
                InlineKeyboardButton(  # Generates a callback query when pressed
                    f"convert to audio({AUDIO_FORMAT})",
                    callback_data="convert"
                )
            ]
        ]
    )
    if result["status"]:
        client.send_chat_action(chat_id, 'upload_document')
        video_paths = result["filepath"]
        bot_msg.edit_text('Download complete. Sending now...')
        for video_path in video_paths:
            # normally there's only one video in that path...
            filename = pathlib.Path(video_path).name
            remain = bot_text.remaining_quota_caption(chat_id)
            st_size = os.stat(video_path).st_size
            size = sizeof_fmt(st_size)
            if st_size > TG_MAX_SIZE:
                t = f"Your video size is {size} which is too large for Telegram. I'll upload it to transfer.sh"
                bot_msg.edit_text(t)
                client.send_chat_action(chat_id, 'upload_document')
                client.send_message(chat_id, upload_transfer_sh(video_paths))
                return

            meta = get_metadata(video_path)
            worker = get_dl_source()
            cap = f"`{filename}`\n\n{url}\n\nInfo: {meta['width']}x{meta['height']} {size} {meta['duration']}s" \
                  f"\n{remain}\n{worker}"
            settings = get_user_settings(str(chat_id))
            if ARCHIVE_ID:
                chat_id = ARCHIVE_ID
            if settings[2] == "document":
                logging.info("Sending as document")
                res_msg = client.send_document(chat_id, video_path,
                                               caption=cap,
                                               progress=upload_hook, progress_args=(bot_msg,),
                                               reply_markup=markup,
                                               thumb=meta["thumb"]
                                               )
            elif settings[2] == "audio":
                logging.info("Sending as audio")
                res_msg = client.send_audio(chat_id, video_path,
                                            caption=cap,
                                            progress=upload_hook, progress_args=(bot_msg,),
                                            )
            else:
                logging.info("Sending as video")
                res_msg = client.send_video(chat_id, video_path,
                                            supports_streaming=True,
                                            caption=cap,
                                            progress=upload_hook, progress_args=(bot_msg,),
                                            reply_markup=markup,
                                            **meta
                                            )
            clink = VIP().extract_canonical_link(url)
            unique = get_unique_clink(clink, settings)
            red.add_send_cache(unique, res_msg.chat.id, res_msg.message_id)
            red.update_metrics("video_success")
            if ARCHIVE_ID:
                client.forward_messages(bot_msg.chat.id, ARCHIVE_ID, res_msg.message_id)
        bot_msg.edit_text('Download success!✅')
    else:
        client.send_chat_action(chat_id, 'typing')
        tb = result["error"][0:4000]
        bot_msg.edit_text(f"Download failed!❌\n\n```{tb}```", disable_web_page_preview=True)

    temp_dir.cleanup()


@Panel.register
def hot_patch(*args):
    git_path = pathlib.Path().cwd().parent
    logging.info("Hot patching on path %s...", git_path)

    pip_install = "pip install -r requirements.txt"
    unset = "git config --unset http.https://github.com/.extraheader"
    pull_unshallow = "git pull origin --unshallow"
    pull = "git pull"

    subprocess.call(pip_install, shell=True, cwd=git_path)
    subprocess.call(unset, shell=True, cwd=git_path)
    if subprocess.call(pull_unshallow, shell=True, cwd=git_path) != 0:
        logging.info("Already unshallow, pulling now...")
        subprocess.call(pull, shell=True, cwd=git_path)

    logging.info("Code is updated, applying hot patch now...")
    psutil.Process().kill()


def run_celery():
    argv = [
        "-A", "tasks", 'worker', '--loglevel=info',
        "--pool=threads", f"--concurrency={WORKERS * 2}",
        "-n", os.getenv("WORKER_NAME", "")
    ]
    app.worker_main(argv)


if __name__ == '__main__':
    celery_client.start()
    print("Bootstrapping Celery worker now.....")
    time.sleep(5)
    threading.Thread(target=run_celery, daemon=True).start()

    scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
    scheduler.add_job(auto_restart, 'interval', seconds=5)
    scheduler.start()

    idle()
    celery_client.stop()
