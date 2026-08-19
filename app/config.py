"""Application configuration, loaded from environment / .env file."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    anthropic_api_key: str = ""
    tavily_api_key: str = ""
    claude_model: str = "claude-sonnet-5"
    database_url: str = "sqlite:///./data/forklifts.db"

    # ---- Auth / access control ----
    session_secret: str = "change-me-in-production"     # signs session cookies
    session_https_only: bool = False                     # True in production (behind HTTPS)
    access_password: str = ""                            # shared review password (view-only); blank = off
    admin_password: str = ""                             # shared admin password (full access); blank = off
    allowed_email_domain: str = ""                       # e.g. "company.com"; blank = any
    admin_emails: str = ""                               # comma-separated bootstrap admins
    dev_login: bool = False                              # local-only test bypass (never in prod)

    # Google OIDC
    google_client_id: str = ""
    google_client_secret: str = ""
    # Microsoft OIDC
    microsoft_client_id: str = ""
    microsoft_client_secret: str = ""
    microsoft_tenant: str = "common"                     # tenant id, or "common"/"organizations"

    @property
    def ai_enabled(self) -> bool:
        """True when Claude is configured (spec extraction + NL->SQL)."""
        return bool(self.anthropic_api_key)

    @property
    def web_lookup_enabled(self) -> bool:
        """True when both web search and extraction are configured."""
        return bool(self.tavily_api_key and self.anthropic_api_key)

    @property
    def password_enabled(self) -> bool:
        return bool(self.access_password or self.admin_password)

    @property
    def google_enabled(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)

    @property
    def microsoft_enabled(self) -> bool:
        return bool(self.microsoft_client_id and self.microsoft_client_secret)

    @property
    def admin_email_set(self) -> set[str]:
        return {e.strip().lower() for e in self.admin_emails.split(",") if e.strip()}

    def email_allowed(self, email: str) -> bool:
        """True if the email may sign in (matches the allowed domain, if set)."""
        email = (email or "").lower()
        if not self.allowed_email_domain:
            return True
        return email.endswith("@" + self.allowed_email_domain.lower())


settings = Settings()
