"""Platform adapters: staged file descriptors, LinkedIn video, IG actions."""
from pathlib import Path

import httpx

import app.platforms as platforms
from app.linkedin import extract_video_urn


class R:
    """Minimal stand-in for a Composio tool result."""

    def __init__(self, data=None, successful=True, error=""):
        self.data = data if data is not None else {}
        self.successful = successful
        self.error = error


class FakeExecute:
    def __init__(self, *results):
        self.results = list(results)
        self.calls: list[tuple[str, str, dict]] = []

    def __call__(self, user_id, toolkit, action, args=None):
        self.calls.append((toolkit, action, args or {}))
        return self.results.pop(0)


def _stage(monkeypatch, descriptor=None):
    staged: list[tuple] = []

    def fake_upload(toolkit, action, path):
        staged.append((toolkit, action, Path(path)))
        return descriptor or {"name": "media.mp4",
                              "mimetype": "video/mp4", "s3key": "s3://key"}

    monkeypatch.setattr(platforms.composio_client, "upload_file", fake_upload)
    return staged


def _video(tmp_path) -> Path:
    p = tmp_path / "clip.mp4"
    p.write_bytes(b"fake video bytes")
    return p


def test_linkedin_text_post_returns_url(monkeypatch):
    ex = FakeExecute(R(data={"post_url": "https://linkedin.example/p/1"}))
    monkeypatch.setattr(platforms.composio_client, "execute", ex)

    url = platforms.publish(1, "linkedin", "urn:li:person:1", "hello")

    assert url == "https://linkedin.example/p/1"
    assert ex.calls == [("LINKEDIN", "LINKEDIN_CREATE_LINKED_IN_POST", {
        "author": "urn:li:person:1", "commentary": "hello",
        "visibility": "PUBLIC"})]


def test_linkedin_image_upload_flow(monkeypatch, tmp_path):
    puts: list[tuple] = []

    class Put:
        def __call__(self, url, content=None, headers=None, timeout=None):
            puts.append((url, content, headers))
            return self

        def raise_for_status(self):
            return None

    monkeypatch.setattr(httpx, "put", Put())
    img = tmp_path / "photo.jpg"
    img.write_bytes(b"jpeg-bytes")
    ex = FakeExecute(
        R(data={"upload_url": "https://upload.example/1",
                "image_urn": "urn:li:image:AAA"}),
        R(data={"post_url": "https://linkedin.example/p/2"}),
    )
    monkeypatch.setattr(platforms.composio_client, "execute", ex)

    url = platforms.publish(1, "linkedin", "urn:li:person:1", "hello",
                            media_path=img, media_type="image")

    assert url == "https://linkedin.example/p/2"
    assert len(puts) == 1 and puts[0][1] == b"jpeg-bytes"
    assert ex.calls[0][1] == "LINKEDIN_INITIALIZE_IMAGE_UPLOAD"
    assert ex.calls[1][2]["images"] == ["urn:li:image:AAA"]


def test_linkedin_video_two_step_upload(monkeypatch, tmp_path):
    staged = _stage(monkeypatch)
    video = _video(tmp_path)
    ex = FakeExecute(
        # data is a JSON string here, exercising the regex fallback.
        R(data='{"video_urn": "urn:li:video:777"}'),
        R(data={"url": "https://linkedin.example/v/1"}),
    )
    monkeypatch.setattr(platforms.composio_client, "execute", ex)

    url = platforms.publish(9, "linkedin", "urn:li:person:9", "caption",
                            media_path=video, media_type="video")

    assert url == "https://linkedin.example/v/1"
    assert staged and staged[0][0:2] == ("LINKEDIN", "LINKEDIN_UPLOAD_VIDEO")
    assert ex.calls[0][1] == "LINKEDIN_UPLOAD_VIDEO"
    assert ex.calls[0][2]["file"]["s3key"] == "s3://key"
    assert ex.calls[1] == ("LINKEDIN", "LINKEDIN_CREATE_VIDEO_POST", {
        "video_urn": "urn:li:video:777", "commentary": "caption",
        "visibility": "PUBLIC"})


def test_linkedin_video_url_skips_staging(monkeypatch):
    staged = _stage(monkeypatch)
    ex = FakeExecute(
        R(data={"video_urn": "urn:li:video:1"}),
        R(data={}),
    )
    monkeypatch.setattr(platforms.composio_client, "execute", ex)

    platforms.publish(9, "linkedin", "urn:li:person:9", "caption",
                      media_url="https://cdn.example/clip.mp4",
                      media_type="video")

    assert staged == []
    assert ex.calls[0][2] == {"video_url": "https://cdn.example/clip.mp4"}


def test_linkedin_org_video_is_a_clear_error(monkeypatch, tmp_path):
    _stage(monkeypatch)
    monkeypatch.setattr(platforms.composio_client, "execute", FakeExecute())

    try:
        platforms.publish(9, "linkedin", "urn:li:organization:5", "caption",
                          media_path=_video(tmp_path), media_type="video")
    except RuntimeError as e:
        assert "personal profile" in str(e)
    else:
        raise AssertionError("expected RuntimeError")


def test_extract_video_urn_shapes():
    assert extract_video_urn(R(data={"video_urn": "urn:li:video:5"})) == \
        "urn:li:video:5"
    assert extract_video_urn(R(data='{"urn": "urn:li:video:6"}')) == \
        "urn:li:video:6"
    assert extract_video_urn(R(data="nothing here")) == ""


def test_twitter_image_uses_staged_descriptor(monkeypatch, tmp_path):
    staged = _stage(monkeypatch, {"name": "p.png", "mimetype": "image/png",
                                  "s3key": "s3://img"})
    img = tmp_path / "p.png"
    img.write_bytes(b"png")
    ex = FakeExecute(R(data={"media_id_string": "123"}), R(data={}))
    monkeypatch.setattr(platforms.composio_client, "execute", ex)

    platforms.publish(3, "twitter", "", "hello",
                      media_path=img, media_type="image")

    assert staged[0][0:2] == ("TWITTER", "TWITTER_UPLOAD_MEDIA")
    assert ex.calls[0][2]["media"]["s3key"] == "s3://img"
    assert ex.calls[0][2]["media_category"] == "tweet_image"
    assert ex.calls[1][2]["media_media_ids"] == ["123"]


def test_twitter_video_polls_before_posting(monkeypatch, tmp_path):
    _stage(monkeypatch)
    ex = FakeExecute(
        R(data={"media_id_string": "55"}),
        R(data={"state": "succeeded"}),
        R(data={}),
    )
    monkeypatch.setattr(platforms.composio_client, "execute", ex)
    monkeypatch.setattr(platforms.time, "sleep", lambda s: None)

    platforms.publish(3, "twitter", "", "hello",
                      media_path=_video(tmp_path), media_type="video")

    actions = [c[1] for c in ex.calls]
    assert actions == ["TWITTER_UPLOAD_LARGE_MEDIA",
                       "TWITTER_GET_MEDIA_UPLOAD_STATUS",
                       "TWITTER_CREATION_OF_A_POST"]


def test_twitter_failed_media_stops_publish(monkeypatch, tmp_path):
    _stage(monkeypatch)
    ex = FakeExecute(R(data={"media_id_string": "55"}),
                     R(data={"state": "failed"}))
    monkeypatch.setattr(platforms.composio_client, "execute", ex)
    monkeypatch.setattr(platforms.time, "sleep", lambda s: None)

    try:
        platforms.publish(3, "twitter", "", "hello",
                          media_path=_video(tmp_path), media_type="video")
    except RuntimeError as e:
        assert "process the media" in str(e)
    else:
        raise AssertionError("expected RuntimeError")


def test_facebook_photo_and_video_field_names(monkeypatch, tmp_path):
    staged = _stage(monkeypatch, {"name": "m", "mimetype": "video/mp4",
                                  "s3key": "s3://fb"})
    ex = FakeExecute(R(data={"id": "page_post_1"}))
    monkeypatch.setattr(platforms.composio_client, "execute", ex)
    img = tmp_path / "p.jpg"
    img.write_bytes(b"jpg")

    platforms.publish(4, "facebook", "12345", "hello",
                      media_path=img, media_type="image")

    assert staged[0][0:2] == ("FACEBOOK", "FACEBOOK_CREATE_PHOTO_POST")
    assert ex.calls[0][2]["photo"]["s3key"] == "s3://fb"
    assert ex.calls[0][2]["page_id"] == "12345"

    ex2 = FakeExecute(R(data={"id": "page_post_2"}))
    monkeypatch.setattr(platforms.composio_client, "execute", ex2)
    platforms.publish(4, "facebook", "12345", "hello",
                      media_path=_video(tmp_path), media_type="video")
    assert ex2.calls[0][1] == "FACEBOOK_CREATE_VIDEO_POST"
    assert ex2.calls[0][2]["video"]["s3key"] == "s3://fb"
    assert ex2.calls[0][2]["description"] == "hello"


def test_instagram_container_and_publish_actions(monkeypatch):
    ex = FakeExecute(R(data={"id": "container-1"}), R(data={"id": "media-1"}))
    monkeypatch.setattr(platforms.composio_client, "execute", ex)

    platforms.publish(5, "instagram", "178414", "caption",
                      media_url="https://cdn.example/i.jpg", media_type="image")

    assert ex.calls[0] == ("INSTAGRAM", "INSTAGRAM_POST_IG_USER_MEDIA", {
        "ig_user_id": "178414", "caption": "caption",
        "image_url": "https://cdn.example/i.jpg"})
    assert ex.calls[1][1] == "INSTAGRAM_POST_IG_USER_MEDIA_PUBLISH"
    assert ex.calls[1][2]["creation_id"] == "container-1"
    assert ex.calls[1][2]["max_wait_seconds"] == 60
