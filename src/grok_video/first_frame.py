"""Optional product-to-scene stage; only explicit image-edit protocol bindings."""
import base64
import io
import os
import warnings

import requests

from . import client, store, product_image
from .output_video import canvas


class FirstFrameError(client.GrokVideoClientError):
    pass


def render(request):
    placement = (
        "The presenter holds exactly one product box at lower chest height, front facing the camera. "
        "Both hands remain steady; there are no pointing, waving or conversational gestures."
        if request["display_mode"] == "handheld" else
        "Place exactly one product box on a stable surface appropriate to the chosen setting, such as "
        "a podium-side stand, home counter, table or outdoor display surface. Keep it clearly visible "
        "beside the presenter, not hidden behind a lectern. The later video action will pick it up once."
    )
    direction = request.get("first_frame_prompt") or (
        "Independently art-direct a scene of ONE person actively introducing this product, "
        "not a product-only still life or a person merely posing beside it. "
        "Choose one coherent setting suited to the product and video description: for example "
        "a product launch event, a presentation stage or lectern, a lived-in home, "
        "a garden, terrace, park or another believable outdoor location. These are alternatives, "
        "not a checklist or a collage; invent another suitable setting when appropriate. "
        "Do not default every request to a showroom, white table, suit or red curtain. "
        "Choose clothing, posture, props and lighting to suit the selected setting, including "
        "natural daylight or believable evening lighting. Keep the presenter and product well lit. "
        "Use a camera-facing, engaged presentation pose, with the product as the subject of the explanation."
    )
    person = request.get("person_description") or (
        "Design a fictional adult presenter suited to this scene. Choose the adult age freely, "
        "including young, middle-aged or older adults, with believable age-specific skin texture. "
        "Do not fix the presenter to one age, gender, hairstyle or appearance. "
        "Honor any explicit presenter requirements in the user's scene or video description."
    )
    return f"""Generate ONE photorealistic scene image for the opening frame of a product video.
Canvas aspect ratio: {request['aspect_ratio']}. Compose for this ratio from the start.
The input image is a PRODUCT ASSET ONLY, not a scene or a layout to animate.
Preserve the product's shape, proportions, colors, packaging and supplied brand elements.
Do not copy the product image background, borders or square layout.
Presenter: {person}. Natural skin texture, realistic anatomy, one consistent adult identity.
Product name (context only, do not add text): {request.get('product_name') or 'the supplied product'}.
{placement}
Creative direction: {direction}
Explicit user requirements take precedence over these creative suggestions.
Video context (use only to inform the still scene, do not render text): {request['prompt']}
Choose a medium or moderately wider shot that reveals the setting while keeping the face,
hands and product clearly visible and large enough to inspect; avoid distant stage-wide shots.
Leave room for the single slow pickup action, not for conversational gestures. Keep the product
unobscured and in believable scale.
No collage, border, black padding, extra people, captions, watermark or invented package text.
This is a still image: do not draw speech, subtitles or motion diagrams."""


def generate(config, request, on_submit=None):
    product = product_image.load(request["product_image_url"])
    width, height = canvas(request["resolution"], request["aspect_ratio"])
    # The image API recommends multiples of 16; prepare() normalizes the final canvas.
    size = f"{round(width / 16) * 16}x{round(height / 16) * 16}"
    payload = {
        "model": config["model"], "prompt": render(request),
        "n": "1", "size": size, "quality": "high",
        "response_format": "b64_json", "output_format": "png",
    }
    if on_submit:
        on_submit()
    try:
        response = requests.post(
            client._url(config, "/v1/images/edits"), data=payload, files={"image": product},
            headers={"Authorization": f"Bearer {config['api_key']}"},
            timeout=(15, config["request_timeout_seconds"]), allow_redirects=False,
        )
    except requests.RequestException:
        raise client.SubmissionUnknown("First-frame submission outcome unknown; check image provider") from None
    try:
        if response.status_code in (408, 202) or response.status_code >= 500:
            raise client.SubmissionUnknown("First-frame submission outcome unknown; do not resubmit automatically")
        if response.status_code != 200:
            raise FirstFrameError(f"First-frame image edit HTTP {response.status_code}")
        try:
            data = response.json()
            encoded = data["data"][0]["b64_json"]
            if not isinstance(encoded, str) or not encoded or len(encoded) > 28_000_000:
                raise ValueError
        except (ValueError, KeyError, TypeError, IndexError):
            raise client.SubmissionUnknown(
                "Image provider returned no usable b64_json; verify protocol and provider result"
            ) from None
        return {"b64_json": encoded}
    finally:
        response.close()


def path_for(task_id):
    root = store.result_root()
    path = (root / f"{task_id}.first-frame.png").resolve()
    if path.parent != root:
        raise FirstFrameError("Invalid first-frame path")
    return path


def prepare(task_id, response, request):
    """Reject a wrong composition ratio; do not disguise a square scene with padding."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    target = path_for(task_id)
    temporary = target.with_suffix(".tmp")
    try:
        raw = base64.b64decode(response["b64_json"], validate=True)
        if len(raw) > 20 * 1024 * 1024:
            raise ValueError
        width, height = canvas(request["resolution"], request["aspect_ratio"])
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as source:
                if source.format not in ("PNG", "JPEG", "WEBP") or getattr(source, "n_frames", 1) != 1:
                    raise ValueError
                if source.width * source.height > 25_000_000:
                    raise ValueError
                image = ImageOps.exif_transpose(source)
                if abs((image.width / image.height) / (width / height) - 1) > 0.02:
                    raise FirstFrameError(
                        "Generated first frame has wrong aspect ratio; video was not submitted"
                    )
                original_size = image.size
                image = image.convert("RGB").resize((width, height), Image.Resampling.LANCZOS)
                target.parent.mkdir(parents=True, exist_ok=True)
                image.save(temporary, format="PNG")
        if temporary.stat().st_size > 10 * 1024 * 1024:
            raise FirstFrameError("Prepared first frame exceeds video input limit of 10 MiB")
        os.replace(temporary, target)
    except (ValueError, KeyError, OSError, UnidentifiedImageError,
            Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise FirstFrameError("Generated first frame is invalid or too large; video was not submitted") from None
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "image_model_id": request["image_model_id"],
        "width": width, "height": height,
        "source_width": original_size[0], "source_height": original_size[1],
        "content_url": f"/openapi/capcut-mate/v1/grok_video_tasks/{task_id}/first_frame",
    }


def video_image(task_id):
    path = path_for(task_id)
    if not path.is_file():
        raise FirstFrameError("Saved first frame is missing; restore it before continuing")
    raw = path.read_bytes()
    if len(raw) > 10 * 1024 * 1024:
        raise FirstFrameError("Saved first frame exceeds video input limit")
    return {"url": "data:image/png;base64," + base64.b64encode(raw).decode("ascii")}
