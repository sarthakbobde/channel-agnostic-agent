"""The signed-in person's profile: department and groups, and a photo that only accepts real raster images."""
from app.admin import api as admin_api
from app.admin import profile_api
from app.db import Group, GroupMember, User
from tests.test_admin import EMAIL, env, login  # noqa: F401  (env is a fixture)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


def _client(env):  # noqa: F811
    ctx, c = env
    c.app.include_router(profile_api.router)
    return ctx, c


def test_profile_shows_department_and_groups(env):  # noqa: F811
    ctx, c = _client(env)
    with ctx.db.session() as s:
        uid = s.query(User).filter_by(email=EMAIL).one().id
        s.add(Group(id=7, name="HR leads"))
        s.add(GroupMember(group_id=7, user_id=uid))
    _, hdr = login(c)
    p = c.get("/admin/api/me/profile").json()
    assert p["email"] == EMAIL and p["groups"] == ["HR leads"] and p["department"] and p["has_avatar"] is False


def test_photo_upload_read_and_remove(env):  # noqa: F811
    ctx, c = _client(env)
    _, hdr = login(c)
    assert c.get("/admin/api/me/avatar").status_code == 404
    r = c.put("/admin/api/me/avatar", files={"file": ("me.png", PNG, "image/png")}, headers=hdr)
    assert r.status_code == 200
    got = c.get("/admin/api/me/avatar")
    assert got.status_code == 200 and got.content == PNG and got.headers["content-type"] == "image/png"
    assert got.headers["x-content-type-options"] == "nosniff"
    assert c.get("/admin/api/me/profile").json()["has_avatar"] is True
    assert c.delete("/admin/api/me/avatar", headers=hdr).status_code == 200
    assert c.get("/admin/api/me/avatar").status_code == 404


def test_photo_rejects_svg_wrong_bytes_big_files_and_missing_csrf(env):  # noqa: F811
    ctx, c = _client(env)
    _, hdr = login(c)
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    assert c.put("/admin/api/me/avatar", files={"file": ("x.svg", svg, "image/svg+xml")}, headers=hdr).status_code == 400
    assert c.put("/admin/api/me/avatar", files={"file": ("x.png", b"not an image", "image/png")}, headers=hdr).status_code == 400
    big = JPEG + b"\x00" * profile_api.MAX_AVATAR
    assert c.put("/admin/api/me/avatar", files={"file": ("x.jpg", big, "image/jpeg")}, headers=hdr).status_code == 413
    assert c.put("/admin/api/me/avatar", files={"file": ("x.jpg", JPEG, "image/jpeg")}).status_code == 403
    assert c.get("/admin/api/me/profile").json()["has_avatar"] is False


def test_profile_needs_sign_in(env):  # noqa: F811
    ctx, c = _client(env)
    assert c.get("/admin/api/me/profile").status_code == 401
    assert admin_api.router is not profile_api.router
