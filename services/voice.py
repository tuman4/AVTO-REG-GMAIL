"""
Voice OTP Server - receives Twilio/Telnyx call recordings, transcribes them and
queues the spoken verification code for the main script to poll.

Security notes:
  - /voice and /otp BOTH require the token. v1 left /otp completely open and
    /voice open by default (an empty VOICE_SERVER_TOKEN short-circuited the
    check), so anyone on the network could read every captured OTP.
  - Downloads of caller-supplied URLs carry a timeout; without one, four slow
    requests exhausted the 4-worker pool permanently.
  - Audio files are cleaned up in a finally block, on every path.
"""
from flask import Flask, request
import speech_recognition as sr
from pydub import AudioSegment
import requests
import os
import re
import time
from queue import Queue, Empty
import logging
from config.settings import Config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("voice_server")

app = Flask(__name__)

otp_queue = Queue()

TEMP_DIR = "temp_audio"
os.makedirs(TEMP_DIR, exist_ok=True)

_AUDIO_DOWNLOAD_TIMEOUT = 20
_RECOGNIZER_TIMEOUT = 30

# Spoken digits ("one two three") are transcribed as words by Google's free
# recognizer; without this map those calls never yielded a code.
_WORD_TO_DIGIT = {
    "zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
}


def _authorized():
    token = Config.VOICE_SERVER_TOKEN
    if not token or token == "changeme":
        # Refuse to serve unauthenticated rather than silently allowing it.
        return False
    return request.args.get("token") == token or request.headers.get("X-Voice-Token") == token


@app.route("/voice", methods=['POST'])
def receive_call():
    """Webhook for Twilio/Telnyx to send call recording. Expects RecordingUrl."""
    if not _authorized():
        logger.warning("Unauthorized access attempt to /voice")
        return "Unauthorized", 401

    local_mp3 = None
    local_wav = None
    try:
        audio_url = request.form.get('RecordingUrl')
        if not audio_url:
            return "No recording URL", 400

        # Twilio appends .mp3 only if the URL does not already carry an extension
        # or query string; v1 produced ".mp3.mp3" and broke signed URLs.
        url = audio_url
        if "." not in url.rsplit("/", 1)[-1].split("?", 1)[0]:
            url += ".mp3"

        audio_response = requests.get(url, timeout=_AUDIO_DOWNLOAD_TIMEOUT)
        if audio_response.status_code != 200:
            audio_response = requests.get(audio_url, timeout=_AUDIO_DOWNLOAD_TIMEOUT)
        if audio_response.status_code != 200:
            return f"Download failed ({audio_response.status_code})", 502

        local_mp3 = os.path.join(TEMP_DIR, f"call_{int(time.time() * 1000)}.mp3")
        with open(local_mp3, "wb") as f:
            f.write(audio_response.content)

        local_wav = local_mp3[:-4] + ".wav"
        AudioSegment.from_mp3(local_mp3).export(local_wav, format="wav")

        recognizer = sr.Recognizer()
        with sr.AudioFile(local_wav) as source:
            audio_data = recognizer.record(source, duration=60)
        text = recognizer.recognize_google(audio_data, timeout=_RECOGNIZER_TIMEOUT)
        logger.info("[+] Transcription received")

        code = _extract_code(text)
        if code:
            logger.info("[+] OTP captured")
            otp_queue.put({"code": code, "timestamp": time.time()})
            return "OK", 200

        logger.info("[-] No OTP found in audio")
        return "No OTP found", 200

    except Exception as e:
        logger.error(f"[-] Error processing call: {e}")
        return "Processing error", 500
    finally:
        # v1 only cleaned up on the success path — every failure and every
        # "no OTP" case left recordings on disk forever.
        for path in (local_mp3, local_wav):
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass


def _extract_code(text):
    """Find the OTP in a transcription: literal digits or spelled-out words."""
    if not text:
        return None

    clean = text.replace(" ", "").replace("-", "")
    match = re.search(r"(?<!\d)(\d{4,8})(?!\d)", clean)
    if match:
        return match.group(1)

    words = re.findall(r"\b(zero|oh|one|two|three|four|five|six|seven|eight|nine)\b",
                       clean.lower())
    if len(words) >= 4:
        return "".join(_WORD_TO_DIGIT[w] for w in words[:8])

    return None


@app.route("/otp", methods=['GET'])
def get_otp():
    """Endpoint for the main script to poll for a captured OTP."""
    if not _authorized():
        logger.warning("Unauthorized access attempt to /otp")
        return "Unauthorized", 401

    # get_nowait, not empty()-then-get(): the check-then-act race let a second
    # concurrent caller block forever on an empty queue and leak a worker.
    try:
        return otp_queue.get_nowait()
    except Empty:
        return {"code": None}


def run_server():
    """Run the server with waitress (production-grade WSGI)."""
    from waitress import serve
    port = int(os.environ.get("VOICE_SERVER_PORT", "5000"))
    host = os.environ.get("VOICE_SERVER_HOST", "127.0.0.1")
    logger.info(f"[*] Voice OTP Server listening on {host}:{port} (waitress)")
    serve(app, host=host, port=port, threads=4)


if __name__ == "__main__":
    run_server()
