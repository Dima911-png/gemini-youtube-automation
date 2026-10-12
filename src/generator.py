# FILE: src/generator.py
# FINAL, CLEAN VERSION: Compatible with per-slide audio sync, dynamic slides, and GitHub Actions.

import os
import json
import time
import random
import requests
from io import BytesIO
from google import genai
from gtts import gTTS
from gtts.tts import gTTSError
from moviepy.editor import AudioFileClip, ImageClip, CompositeAudioClip, CompositeVideoClip, concatenate_videoclips, vfx
import numpy as np
from moviepy.config import change_settings
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from pathlib import Path
from pydub import AudioSegment

# --- Configuration ---
ASSETS_PATH = Path("assets")
FONT_FILE = ASSETS_PATH / "fonts/arial.ttf"
BACKGROUND_MUSIC_PATH = ASSETS_PATH / "music/bg_music.mp3"
FALLBACK_THUMBNAIL_FONT = ImageFont.load_default()
YOUR_NAME = "Подготовка к ОГЭ"

# Google's TTS endpoint throttles bursts from shared CI IPs. When it does, it answers
# 200 OK with no audio stream, which gTTS surfaces as "Probable cause: Unknown".
TTS_MAX_ATTEMPTS = 5
TTS_BACKOFF_SECONDS = 5
TTS_MIN_GAP_SECONDS = 2
_last_tts_request = 0.0

# GitHub Actions compatibility for ImageMagick
if os.name == 'posix':
    change_settings({"IMAGEMAGICK_BINARY": "/usr/bin/convert"})


def get_pexels_image(query, video_type):
    """Searches for a relevant image on Pexels and returns the image object."""
    pexels_api_key = os.getenv("PEXELS_API_KEY")
    if not pexels_api_key:
        print("⚠️ PEXELS_API_KEY not found. Using solid color background.")
        return None

    orientation = 'landscape' if video_type == 'long' else 'portrait'
    try:
        headers = {"Authorization": pexels_api_key}
        params = {"query": f"abstract {query}", "per_page": 1, "orientation": orientation}
        response = requests.get("https://api.pexels.com/v1/search", headers=headers, params=params, timeout=15)
        response.raise_for_status()
        data = response.json()
        if data.get('photos'):
            image_url = data['photos'][0]['src']['large2x']
            image_response = requests.get(image_url, timeout=15)
            image_response.raise_for_status()
            return Image.open(BytesIO(image_response.content)).convert("RGBA")
    except requests.exceptions.RequestException as e:
        print(f"❌ Network error fetching Pexels image for query '{query}': {e}")
    except Exception as e:
        print(f"❌ General error fetching Pexels image for query '{query}': {e}")
    return None


def _throttle_tts():
    """Spaces out TTS requests so a burst doesn't trip Google's rate limiter."""
    global _last_tts_request
    elapsed = time.monotonic() - _last_tts_request
    if elapsed < TTS_MIN_GAP_SECONDS:
        time.sleep(TTS_MIN_GAP_SECONDS - elapsed)
    _last_tts_request = time.monotonic()


def text_to_speech(text, output_path):
    """Converts text to speech using gTTS and ensures clean audio using WAV format."""
    print(f"🎤 Converting script to speech...")
    temp_mp3_path = str(output_path).replace('.mp3', '_temp.mp3')
    wav_path = str(output_path.with_suffix('.wav'))

    for attempt in range(1, TTS_MAX_ATTEMPTS + 1):
        try:
            _throttle_tts()
            tts = gTTS(text=text, lang='ru', slow=False)
            tts.save(temp_mp3_path)

            # A throttled response can still produce a file — just an empty/truncated one.
            if os.path.getsize(temp_mp3_path) < 1024:
                raise gTTSError("gTTS wrote an empty or truncated audio file.")

            audio = AudioSegment.from_mp3(temp_mp3_path)
            audio.export(wav_path, format="wav", codec="pcm_s16le")
            os.remove(temp_mp3_path)

            print(f"✅ Speech generated and converted to WAV successfully!")
            return Path(wav_path)

        except Exception as e:
            if os.path.exists(temp_mp3_path):
                os.remove(temp_mp3_path)

            if attempt == TTS_MAX_ATTEMPTS:
                print(f"❌ ERROR: Failed to generate speech after {TTS_MAX_ATTEMPTS} attempts: {e}")
                raise

            delay = TTS_BACKOFF_SECONDS * (2 ** (attempt - 1)) + random.uniform(0, 2)
            print(f"⚠️ TTS attempt {attempt}/{TTS_MAX_ATTEMPTS} failed ({e}). Retrying in {delay:.1f}s...")
            time.sleep(delay)



def _generate_content_with_fallback(client, prompt):
    """Call Gemini with bounded retries and fallback models when capacity is unavailable."""
    models = [
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
        "gemini-3.8-flash",
    ]
    last_error = None
    for model in models:
        for attempt in range(1, 3):
            try:
                print(f"🤖 Requesting Gemini model {model} (attempt {attempt}/2)...")
                response = client.models.generate_content(model=model, contents=prompt)
                if not getattr(response, "text", None):
                    raise RuntimeError(f"Gemini model {model} returned an empty response.")
                return response
            except Exception as exc:
                last_error = exc
                status = getattr(exc, "status_code", None)
                message = str(exc).lower()
                transient = status in (408, 429, 500, 502, 503, 504) or any(
                    token in message for token in (
                        "unavailable", "high demand", "resource_exhausted",
                        "too many requests", "deadline exceeded", "not found",
                        "model is not available", "not supported for generatecontent"
                    )
                )
                # Authentication/configuration errors won't improve by retrying.
                if status in (400, 401, 403):
                    print(f"⚠️ Model {model} rejected the request (HTTP {status}); trying fallback if available.")
                    break
                if attempt < 2 and transient:
                    delay = 5 * attempt + random.uniform(0, 2)
                    print(f"⚠️ Gemini {model} temporarily failed: {exc}. Retrying in {delay:.1f}s...")
                    time.sleep(delay)
                else:
                    print(f"⚠️ Gemini {model} failed: {exc}.")
                    break
    raise RuntimeError(f"All Gemini models failed. Last error: {last_error}") from last_error


def generate_curriculum(previous_titles=None):
    """Generates the entire course curriculum using Gemini."""
    print("🤖 No content plan found. Generating a new curriculum from scratch...")
    try:
        client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])

        #Optional: Add prior lesson titles for continuation
        history = ""
        if previous_titles:
            formatted = "\n".join([f"{i+1}. {t}" for i, t in enumerate(previous_titles)])
            history = f"Уже созданные уроки:\n{formatted}\n\nПродолжи программу без повторения тем.\n"

        prompt = f"""
        Ты — опытный преподаватель математики. Составь программу русскоязычного YouTube-курса «Подготовка к ОГЭ по математике».
        {history}
        Пиши на русском языке для учеников 8–9 класса. Покрой дроби, проценты, вычисления, текстовые задачи, уравнения, графики и функции, вероятность, статистику, геометрию, практические задачи и разбор вариантов ОГЭ. Иди от простого к сложному, не повторяй темы.

        Respond with ONLY a valid JSON object. The object must contain a key "lessons" which is a list of 20 lesson objects.
        Each lesson object must have these keys: "chapter", "part", "title", "status" (defaulted to "pending"), and "youtube_id" (defaulted to null).
        """
        response = _generate_content_with_fallback(client, prompt)
        json_string = response.text.strip().replace("```json", "").replace("```", "")
        curriculum = json.loads(json_string)
        print("✅ New curriculum generated successfully!")
        return curriculum
    except Exception as e:
        print(f"❌ CRITICAL ERROR: Failed to generate curriculum. {e}")
        raise


def generate_lesson_content(lesson_title):
    """Generates the content for one long-form lesson and its promotional short."""
    print(f"🤖 Generating content for lesson: '{lesson_title}'...")
    try:
        client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])
        prompt = f"""
        Ты — преподаватель математики, готовящий ученика 8–9 класса к ОГЭ. Создай НОВЫЙ оригинальный урок на русском языке по теме «{lesson_title}». Объясняй последовательно и простыми словами. Для каждой задачи дай условие, правило или формулу, пошаговое решение и проверку ответа. Следи за математической точностью.

        ВАЖНО ДЛЯ УНИКАЛЬНОСТИ:
        - Не используй шаблонный вводный текст, одинаковые примеры или повторяющиеся числа из других уроков.
        - Подбери новые конкретные числа, условия задач и способ объяснения именно для этой темы.
        - Каждый слайд должен содержать полезное объяснение или отдельный шаг решения, а не общие фразы.
        - Не повторяй одну и ту же задачу в разных слайдах.
        - Создай полноценный самостоятельный урок, строго связанный с указанной темой.

        Generate a JSON response with three keys:
        1. "long_form_slides": 7–8 слайдов подробного урока. Каждый объект имеет ключи "title" и "content"; весь текст на русском языке с примерами и пошаговыми решениями.
        2. "short_form_highlight": Короткий интересный анонс на русском языке из 1–2 предложений для YouTube Shorts.
        3. "hashtags": Строка из 5–7 хештегов на русском языке, например "#ОГЭ #ОГЭматематика #математика #подготовкакОГЭ #разборзаданий".

        Return only valid JSON.
        """
        response = client.models.generate_content(model='gemini-3.8-flash', contents=prompt)
        json_string = response.text.strip().replace("```json", "").replace("```", "")
        content = json.loads(json_string)
        print("✅ Lesson content generated successfully.")
        return content
    except Exception as e:
        print(f"❌ ERROR: Failed to generate lesson content: {e}")
        raise


# def generate_visuals(output_dir, video_type, slide_content=None, thumbnail_title=None, slide_number=0, total_slides=0):
#     """Generates a single professional, PPT-style slide or a thumbnail."""
#     output_dir.mkdir(exist_ok=True, parents=True)
#     is_thumbnail = thumbnail_title is not None

#     width, height = (1920, 1080) if video_type == 'long' else (1080, 1920)
#     title = thumbnail_title if is_thumbnail else slide_content.get("title", "")
#     bg_image = get_pexels_image(title, video_type)

#     if not bg_image:
#         bg_image = Image.new('RGBA', (width, height), color=(12, 17, 29))
#     bg_image = bg_image.resize((width, height)).filter(ImageFilter.GaussianBlur(5))
#     darken_layer = Image.new('RGBA', bg_image.size, (0, 0, 0, 150))
#     final_bg = Image.alpha_composite(bg_image, darken_layer).convert("RGB")
#     if is_thumbnail and video_type == 'long':
#         w, h = final_bg.size
#         if h > w:
#             print("⚠️ Detected vertical thumbnail for long video. Rotating and resizing to 1920x1080...")
#             final_bg = final_bg.transpose(Image.ROTATE_270).resize((1920, 1080))
#     draw = ImageDraw.Draw(final_bg)

#     try:
#         title_font = ImageFont.truetype(str(FONT_FILE), 80 if video_type == 'long' else 90)
#         content_font = ImageFont.truetype(str(FONT_FILE), 45 if video_type == 'long' else 55)
#         footer_font = ImageFont.truetype(str(FONT_FILE), 25 if video_type == 'long' else 35)
#     except IOError:
#         title_font = content_font = footer_font = FALLBACK_THUMBNAIL_FONT

#     if not is_thumbnail:
#         header_height = int(height * 0.18)
#         draw.rectangle([0, 0, width, header_height], fill=(238, 231, 216))
#         title_bbox = draw.textbbox((0, 0), title, font=title_font)
#         title_x = (width - (title_bbox[2] - title_bbox[0])) / 2
#         title_y = (header_height - (title_bbox[3] - title_bbox[1])) / 2
#         draw.text((title_x, title_y), title, font=title_font, fill=(255, 255, 255))
#     else:
#         title_bbox = draw.textbbox((0, 0), title, font=title_font)
#         title_x = (width - (title_bbox[2] - title_bbox[0])) / 2
#         title_y = (height - (title_bbox[3] - title_bbox[1])) / 2
#         draw.text((title_x, title_y), title, font=title_font, fill=(255, 255, 255), stroke_width=2, stroke_fill="black")

#     if not is_thumbnail:
#         content = slide_content.get("content", "")
#         is_special_slide = len(content.split()) < 10

#         words = content.split()
#         lines = []
#         current_line = ""
#         for word in words:
#             test_line = f"{current_line} {word}".strip()
#             if draw.textbbox((0, 0), test_line, font=content_font)[2] < width * 0.85:
#                 current_line = test_line
#             else:
#                 lines.append(current_line)
#                 current_line = word
#         lines.append(current_line)

#         line_height = content_font.getbbox("A")[3] + 15
#         total_text_height = len(lines) * line_height
#         y_text = (height - total_text_height) / 2 if is_special_slide else header_height + 100

#         for line in lines:
#             line_bbox = draw.textbbox((0, 0), line, font=content_font)
#             line_x = (width - (line_bbox[2] - line_bbox[0])) / 2
#             draw.text((line_x, y_text), line, font=content_font, fill=(230, 230, 230))
#             y_text += line_height

#         footer_height = int(height * 0.06)
#         draw.rectangle([0, height - footer_height, width, height], fill=(25, 40, 65, 200))
#         draw.text((40, height - footer_height + 12), f"{YOUR_NAME}", font=footer_font, fill=(180, 180, 180))
#         if total_slides > 0:
#             slide_num_text = f"Слайд {slide_number} из {total_slides}"
#             slide_num_bbox = draw.textbbox((0, 0), slide_num_text, font=footer_font)
#             draw.text((width - slide_num_bbox[2] - 40, height - footer_height + 12), slide_num_text, font=footer_font, fill=(180, 180, 180))
#     file_prefix = "thumbnail" if is_thumbnail else f"slide_{slide_number:02d}"
#     path = output_dir / f"{file_prefix}.png"
#     final_bg.save(path)
#     return str(path)

def generate_visuals(output_dir, video_type, slide_content=None, thumbnail_title=None, slide_number=0, total_slides=0):
    """Generates a single professional, PPT-style slide or a thumbnail with corrected alignment."""
    output_dir.mkdir(exist_ok=True, parents=True)
    is_thumbnail = thumbnail_title is not None

    width, height = (1920, 1080) if video_type == 'long' else (1080, 1920)
    title = thumbnail_title if is_thumbnail else slide_content.get("title", "")
    bg_image = get_pexels_image(title, video_type)

    # Bright, clean educational palette. Keep backgrounds light even when Pexels is unavailable.
    if not bg_image:
        bg_image = Image.new('RGB', (width, height), color=(250, 248, 243))
    else:
        bg_image = bg_image.convert("RGB")
    bg_image = bg_image.resize((width, height)).filter(ImageFilter.GaussianBlur(2))
    # Wash out busy photos with a strong warm-white overlay for comfortable text contrast.
    light_overlay = Image.new("RGB", bg_image.size, (250, 248, 243))
    final_bg = Image.blend(bg_image, light_overlay, 0.84)

    if is_thumbnail and video_type == 'long':
        w, h = final_bg.size
        if h > w:
            print("⚠️ Detected vertical thumbnail for long video. Rotating and resizing to 1920x1080...")
            final_bg = final_bg.transpose(Image.ROTATE_270).resize((1920, 1080))

    draw = ImageDraw.Draw(final_bg)

    try:
        title_font = ImageFont.truetype(str(FONT_FILE), 80 if video_type == 'long' else 90)
        content_font = ImageFont.truetype(str(FONT_FILE), 45 if video_type == 'long' else 55)
        footer_font = ImageFont.truetype(str(FONT_FILE), 25 if video_type == 'long' else 35)
    except IOError:
        title_font = content_font = footer_font = FALLBACK_THUMBNAIL_FONT

    if not is_thumbnail:
        # Header background
        header_height = int(height * 0.18)
        draw.rectangle([0, 0, width, header_height], fill=(25, 40, 65, 200))

        # Wrap title text if needed
        words = title.split()
        title_lines = []
        current_line = ""
        for word in words:
            test_line = f"{current_line} {word}".strip()
            bbox = draw.textbbox((0, 0), test_line, font=title_font)
            if bbox[2] - bbox[0] < width * 0.9:
                current_line = test_line
            else:
                title_lines.append(current_line)
                current_line = word
        title_lines.append(current_line)

        # Center vertically in header
        line_height = title_font.getbbox("A")[3] + 10
        total_title_height = len(title_lines) * line_height
        y_text = (header_height - total_title_height) / 2

        for line in title_lines:
            bbox = draw.textbbox((0, 0), line, font=title_font)
            x = (width - (bbox[2] - bbox[0])) / 2
            draw.text((x, y_text), line, font=title_font, fill=(48, 57, 68))
            y_text += line_height
    else:
        # Center title on thumbnail
        bbox = draw.textbbox((0, 0), title, font=title_font)
        x = (width - (bbox[2] - bbox[0])) / 2
        y = (height - (bbox[3] - bbox[1])) / 2
        # Warm ivory thumbnail with a dark readable title.
        draw.rounded_rectangle((width * 0.06, height * 0.25, width * 0.94, height * 0.75), radius=36, fill=(250, 248, 243), outline=(214, 173, 112), width=5)
        bbox = draw.textbbox((0, 0), title, font=title_font)
        x = (width - (bbox[2] - bbox[0])) / 2
        y = (height - (bbox[3] - bbox[1])) / 2
        draw.text((x, y), title, font=title_font, fill=(48, 57, 68))

    if not is_thumbnail:
        # Main content block
        content = slide_content.get("content", "")
        is_special_slide = len(content.split()) < 10

        words = content.split()
        lines = []
        current_line = ""
        for word in words:
            test_line = f"{current_line} {word}".strip()
            if draw.textbbox((0, 0), test_line, font=content_font)[2] < width * 0.85:
                current_line = test_line
            else:
                lines.append(current_line)
                current_line = word
        lines.append(current_line)

        line_height = content_font.getbbox("A")[3] + 15
        total_text_height = len(lines) * line_height
        y_text = (height - total_text_height) / 2 if is_special_slide else header_height + 100

        for line in lines:
            bbox = draw.textbbox((0, 0), line, font=content_font)
            x = (width - (bbox[2] - bbox[0])) / 2
            draw.text((x, y_text), line, font=content_font, fill=(48, 57, 68))
            y_text += line_height

        # Footer
        footer_height = int(height * 0.06)
        draw.rectangle([0, height - footer_height, width, height], fill=(238, 231, 216))
        draw.text((40, height - footer_height + 12), f"{YOUR_NAME}", font=footer_font, fill=(95, 101, 108))

        if total_slides > 0:
            slide_num_text = f"Слайд {slide_number} из {total_slides}"
            bbox = draw.textbbox((0, 0), slide_num_text, font=footer_font)
            draw.text((width - bbox[2] - 40, height - footer_height + 12), slide_num_text, font=footer_font, fill=(95, 101, 108))

    file_prefix = "thumbnail" if is_thumbnail else f"slide_{slide_number:02d}"
    path = output_dir / f"{file_prefix}.png"
    final_bg.save(path)
    return str(path)

def _subtitle_image(text, video_type):
    """Create a styled subtitle panel for Russian narration."""
    width, height = (1920, 150) if video_type == "long" else (1080, 190)
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    margin = 28 if video_type == "long" else 22
    draw.rounded_rectangle((margin, 8, width - margin, height - 8), radius=22, fill=(250, 248, 243, 242), outline=(214, 173, 112, 255), width=3)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 38 if video_type == "long" else 34)
    except OSError:
        font = ImageFont.load_default()
    words, lines, current = str(text).split(), [], ""
    max_width = width - 2 * margin - 44
    for word in words:
        candidate = (current + " " + word).strip()
        if draw.textbbox((0, 0), candidate, font=font)[2] <= max_width:
            current = candidate
        else:
            if current: lines.append(current)
            current = word
    if current: lines.append(current)
    line_height = 43
    y = max(12, (height - min(2, len(lines)) * line_height) // 2)
    for line in lines[:2]:
        bbox = draw.textbbox((0, 0), line, font=font)
        x = (width - (bbox[2] - bbox[0])) // 2
        draw.text((x + 1, y + 1), line, font=font, fill=(255, 255, 255, 255))
        draw.text((x, y), line, font=font, fill=(48, 57, 68, 255))
        y += line_height
    return np.asarray(image)


def create_video(slide_paths, audio_paths, output_path, video_type, subtitle_texts=None):
    """Render the video with readable, styled subtitles and original narration quality."""
    print(f"🎬 Creating {video_type} video...")
    if not slide_paths or not audio_paths or len(slide_paths) != len(audio_paths):
        raise ValueError("Mismatch between slides and audio clips, or no slides provided.")
    subtitle_texts = subtitle_texts or [""] * len(slide_paths)
    clips = []
    for img_path, audio_path, narration in zip(slide_paths, audio_paths, subtitle_texts):
        audio = AudioFileClip(str(audio_path))
        duration = audio.duration + 0.5
        base = ImageClip(img_path).set_duration(duration).set_audio(audio).fadein(0.35).fadeout(0.35)
        words = str(narration or "").split()
        chunks = [" ".join(words[i:i + 7]) for i in range(0, len(words), 7)]
        overlays = []
        if chunks:
            weights = [max(1, len(chunk)) for chunk in chunks]
            total = sum(weights)
            cursor = 0.0
            for chunk, weight in zip(chunks, weights):
                chunk_duration = audio.duration * weight / total
                overlay = ImageClip(_subtitle_image(chunk, video_type)).set_start(cursor).set_duration(chunk_duration).set_position(("center", "bottom"))
                overlays.append(overlay)
                cursor += chunk_duration
        clips.append(CompositeVideoClip([base] + overlays, size=base.size).set_duration(duration) if overlays else base)
    final_video = concatenate_videoclips(clips, method="compose")
    if BACKGROUND_MUSIC_PATH.exists():
        print("🎵 Adding quiet background music...")
        music = AudioFileClip(str(BACKGROUND_MUSIC_PATH)).volumex(0.035)
        music = music.fx(vfx.loop, duration=final_video.duration) if music.duration < final_video.duration else music.subclip(0, final_video.duration)
        final_video = final_video.set_audio(CompositeAudioClip([final_video.audio.volumex(1.0), music]))
    final_video.write_videofile(str(output_path), fps=24, codec="libx264", audio_codec="aac", audio_bitrate="192k", preset="medium", threads=4)
    print(f"✅ {video_type.capitalize()} video created successfully!")