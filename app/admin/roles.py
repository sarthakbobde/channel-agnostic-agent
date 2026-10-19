"""Who may do what in the admin panel. Default-deny: a URL path that is not listed for a role is refused,
so a new admin endpoint is Admin-only until it is added here on purpose."""

API = "/admin/api"

# Paths every signed-in panel user may use (their own session and account).
COMMON = (f"{API}/auth", f"{API}/me")

# Extra path prefixes an Editor may use. Finer rules (only their own departments) live in departments_api.
PREFIXES = {
    "admin": (API,),
    "editor": COMMON + (f"{API}/departments", f"{API}/kbs", f"{API}/documents", f"{API}/groups", f"{API}/directory", f"{API}/prompts"),
    "viewer": (),                      # Viewers are people the assistant answers; they have no panel
}

# Screens (tabs) shown in the panel for each role.
SCREENS = {
    "admin": ("channels", "settings", "providers", "knowledge", "groups", "prompts", "guardrails", "departments", "users", "audit", "account"),
    "editor": ("knowledge", "groups", "prompts", "departments", "account"),
    "viewer": (),
}


def allowed(role: str, path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in PREFIXES.get(role, ()))


def screens_for(role: str) -> list[str]:
    return list(SCREENS.get(role, ()))


def can_manage_everything(role: str) -> bool:
    return role == "admin"
