"""Deterministic prompt template; never calls an analysis model."""
from .schemas import GrokVideoTaskRequest


def render(request: GrokVideoTaskRequest) -> str:
    if request.prompt is not None:
        return request.prompt
    duration = request.duration
    a, b, c = (round(duration * fraction, 2) for fraction in (3 / 17, 8 / 17, 13 / 17))
    if request.display_mode == "handheld":
        placement = (
            "Hold exactly one reference product box steadily at lower chest height, front facing camera. "
            "Do not swap hands, change grip, point at it, wave, or make any other hand gesture. "
            "The product box remains still and readable."
        )
        gestures = (
            "No hand gestures. Keep both arms and the product box almost still, with only tiny "
            "natural settling movement."
        )
    else:
        placement = (
            "Start with exactly one reference product box on a stable surface. Perform one deliberate, "
            "slow action only: pick up the box once, bring it to lower chest height, and hold it "
            "steady facing camera. Do not put it down again, point at it, open it, rotate it, "
            "swap hands, or add duplicate packages."
        )
        gestures = (
            "No conversational hand gestures. Apart from the single pickup, keep the hands, arms "
            "and product box steady; only tiny natural settling movement is allowed."
        )
    presenter = f"A newly designed fictional adult female presenter: {request.person_description}."
    if request.product_image_url:
        presenter = f"The fictional adult presenter from the composed first frame: {request.person_description}."
        placement = (
            "Keep the product in the same hand and grip as the first frame. Do not change hands, "
            "point, wave or add any other hand gesture. Preserve the product's appearance and "
            "do not add duplicate products."
            if request.display_mode == "handheld" else
            "Keep exactly one product box on its original surface in the composed first frame. "
            "Pick it up once, slowly, then hold it at lower chest height facing camera. Do not "
            "put it down, rotate it, change hands, point, wave or add duplicate products."
        )
    references = ""
    if request.product_reference_image_urls:
        references = (
            "\nADDITIONAL PRODUCT REFERENCE VIEWS: These URLs show alternate views of the same "
            "product packaging. Use them only to keep the box shape, colors, logo placement and "
            "label details consistent; never create multiple boxes or combine views into a collage.\n"
            + "\n".join(request.product_reference_image_urls)
        )
    text = f"""Create a {duration}-second realistic vertical talking-head product explanation,
aspect ratio {request.aspect_ratio}, one continuous camera take with natural timing.

PRESENTER: {presenter}
The presenter is a believable adult Chinese professional with realistic age-specific skin,
facial asymmetry and body proportions. Keep one identity, hairstyle, clothing and body shape
throughout. Use the clothing, age and styling from the description; if unspecified, choose one
simple professional solid-color outfit appropriate to the setting. Do not force a magenta blazer,
white shirt, brooch, suit, red curtain or studio uniform. No beauty-filter skin or mannequin pose.

PRODUCT: {request.product_name}.
Use the supplied image for product appearance: preserve shape, proportions, colors,
packaging structure and supplied brand elements. Do not invent extra text or claims.
If no brand elements are supplied, use clean packaging without readable invented text.
Reference product appearance overrides any default packaging color.
{placement}
{references}

SCENE: Select one coherent, believable setting that matches the presenter and script, such as
a tidy office or consultation desk, a small lecture stage, a clinic-style consultation room,
or a calm home/workspace. These are alternatives, not a checklist. Use real background depth
and a few relevant objects; avoid a repeated product wall, oversized advertising text or a
generic showroom. Compose for vertical video from the start: eye-level or slightly above,
medium close-up/medium shot, face and upper torso prominent, hands visible when gesturing,
product visible without blocking the face; hands only need to be visible during the single pickup.
Use soft directional practical light and natural
exposure, with mild background separation but no artificial blur halo. Keep the camera mostly
stable; allow only a subtle human-operated drift, no dramatic zoom, pan, cuts or transitions.

PERFORMANCE:
0-{a:g}s: Establish the presenter already settled in the setting; make eye contact, breathe once,
and begin speaking without an abrupt pose change. If the product starts on the surface, leave it
there until the single pickup.
{a:g}-{b:g}s: Pick up the product box once, slowly and deliberately, without any flourish. After
the pickup, keep it at lower chest height and facing camera.
{b:g}-{c:g}s: Continue speaking with the product held steady. Do not add gestures, pointing,
hand switching, opening, rotating or repeated pickup actions.
{c:g}-{duration}s: Finish the final sentence, keep the box stable, and hold a natural attentive
expression for a moment.
{gestures}
Natural breathing, blinking, tiny posture adjustments, small changes of eye focus and slight
weight shifts while speaking. The single pickup must be continuous, anatomically plausible and
completed once; never snap, loop, freeze or reset. Do not make the presenter statue-still, but
also do not invent gestures. Never cover the face, logo or product label.

SPEECH: Natural clear Mandarin Chinese, confident and friendly, lips synchronized to speech.
Speak this copy exactly, finishing naturally within {duration} seconds:
{request.script}

AVOID: face deformation, identity changes, age changes, malformed fingers, extra fingers,
lip-sync mismatch, silent mouth flapping, exaggerated expressions, any hand gesture besides the
single pickup, pointing, waving, hand switching, repeated pickup loops, frozen hands, drifting products,
duplicate products, deformed packaging,
garbled background text, fake medical claims, extra people, subtitles, captions, watermarks,
beauty-filter plastic skin, oversharpening or background music."""
    if request.product_image_url:
        text = text.replace(
            "Keep one identity, hairstyle, clothing and body shape\n"
            "throughout. Use the clothing, age and styling from the description; if unspecified, choose one\n"
            "simple professional solid-color outfit appropriate to the setting. Do not force a magenta blazer,\n"
            "white shirt, brooch, suit, red curtain or studio uniform. No beauty-filter skin or mannequin pose.",
            "Preserve the face, hairstyle, body, clothing and lighting of the presenter in the composed "
            "first frame. Do not replace the identity or restyle the person.",
        )
        start = text.index("SCENE:")
        end = text.index("\nPERFORMANCE:", start)
        text = text[:start] + (
            "SCENE: Preserve the composed first frame's setting, lighting, product placement and framing. "
            "Keep the same vertical composition and camera distance; allow only subtle natural movement. "
            "No zoom, pan, cuts or transitions.\n"
        ) + text[end:]
    return text
