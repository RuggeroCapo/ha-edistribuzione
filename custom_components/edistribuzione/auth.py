"""Authentication client for E-Distribuzione.

Standard OAuth2 Authorization Code + PKCE with the official app's public
client, but the login itself happens in the user's own browser, never here:

    1. crea_link_autorizzazione()  builds the /oauth2/authorize URL (PKCE
                                    challenge + state) for the user to open.
    2. The user logs in on E-Distribuzione's site as usual (email, password,
       OTP), THEN opens the link: with the session active it goes straight to
       the consent page. After "Consenti" the page calls
       handleRedirect('eneldist://redirect?code=...&state=...') in JavaScript,
       which the browser silently ignores (it is the app's scheme), leaving a
       blank page: the user copies that URL from the page source.
    3. async_scambia_url_redirect() checks the pasted URL and exchanges the
       code (with the PKCE verifier) for access_token + refresh_token.

Home Assistant never sees the password or the OTP, and there is no scraping of
the portal's pages: only the two standard OAuth2 endpoints are used.

The portal answers on two hostnames of the same Salesforce org: the custom one
(private.e-distribuzione.it) is behind Imperva, which blocks non-browser
clients, so the token endpoint falls back to the canonical domain
(edistribuzione.my.site.com) when it returns the anti-bot page. The link opened
in the browser always uses the custom domain: a real browser passes the check.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import secrets
from dataclasses import dataclass, field
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

import aiohttp

from .const import (
    OAUTH_AUTHORIZE_URL,
    OAUTH_CLIENT_ID,
    OAUTH_REDIRECT_URI,
    OAUTH_SCOPE,
    OAUTH_TOKEN_URL,
    SF_HOST_DIRETTO,
    SF_HOST_PRINCIPALE,
    SF_HOSTS,
)

_LOGGER = logging.getLogger(__name__)

# Pages of Imperva's (Incapsula) anti-bot protection, which answers in place of
# the token endpoint when it classifies the traffic as automated: the "Pardon
# Our Interruption" JavaScript check (HTTP 200) and the "Incapsula incident ID"
# block.
_MARCATORI_ANTIBOT = (
    "pardon our interruption",
    "incapsula incident id",
)


def _contiene(testo: str, marcatori: tuple[str, ...]) -> bool:
    minuscolo = testo.lower()
    return any(marcatore in minuscolo for marcatore in marcatori)


class AuthError(Exception):
    """Generic authentication error."""


class AccessoBloccato(AuthError):
    """E-Distribuzione answered with Imperva's anti-bot page instead of the
    expected one: automated access is blocked server-side.

    It is not a credentials problem, and there is nothing to fix here: it must
    be reported clearly and retried later, without insisting.
    """


class AppBloccata(AuthError):
    """The token endpoint answered "app_blocked": a Salesforce Transaction
    Security policy is rejecting the OAuth client the integration uses (the
    official app's).

    It is a decision on E-Distribuzione's side, like the anti-bot check: the
    refresh_token is not revoked and a new login does not get past it, so it
    must be reported and retried later, not turned into a reauth.
    """


class TokenNonValido(AuthError):
    """The token endpoint answered "invalid_grant": the refresh_token (or the
    authorization code) is expired, revoked or already used. Only a new login
    in the browser fixes it."""


class ParsingError(AuthError):
    """A response (or the pasted URL) did not contain what was expected."""


class UrlRedirectNonValido(AuthError):
    """The pasted text is not an eneldist://redirect URL carrying a code."""


class StateNonCorrispondente(AuthError):
    """The pasted URL belongs to another authorization (a different link, or an
    older attempt): its 'state' does not match the one generated here."""


class AutorizzazioneNegata(AuthError):
    """The portal redirected with an OAuth error instead of a code, e.g.
    access_denied when the consent was denied."""

    def __init__(self, codice: str, descrizione: str | None) -> None:
        super().__init__(f"{codice}" + (f" ({descrizione})" if descrizione else ""))
        self.codice = codice
        self.descrizione = descrizione


def _errore_oauth(codice: str | None, descrizione: str | None, contesto: str) -> AuthError:
    """Exception for an error answered by the token endpoint as JSON
    ({"error": ..., "error_description": ...})."""
    dettaglio = f"{contesto}: {codice}" + (f" ({descrizione})" if descrizione else "")
    if codice == "app_blocked":
        return AppBloccata(dettaglio)
    if codice == "invalid_grant":
        return TokenNonValido(dettaglio)
    return AuthError(dettaglio)


async def _leggi_testo(resp: aiohttp.ClientResponse) -> str:
    """Response body, after ruling out the anti-bot page."""
    testo = await resp.text()
    if _contiene(testo, _MARCATORI_ANTIBOT):
        raise AccessoBloccato("E-Distribuzione ha risposto con la pagina antibot (Imperva)")
    return testo


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _make_pkce_pair() -> tuple[str, str]:
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


# The URL ends at the first quote, bracket or whitespace: it is pasted from a JS
# string in the page source or from a console message.
_RE_URL_REDIRECT = re.compile(re.escape(OAUTH_REDIRECT_URI) + r"[^'\"<>()\s]*")


def estrai_codice(testo_incollato: str) -> tuple[str, str | None]:
    """(code, state) from what the user pasted.

    Accepts the whole eneldist://redirect?code=...&state=... URL - also with
    text around it, e.g. a piece of the page source - or the bare code. The code is returned
    decoded: Salesforce percent-encodes its trailing '=='.
    """
    testo = testo_incollato.strip().strip("'\"")
    trovato = _RE_URL_REDIRECT.search(testo)
    if trovato is None:
        # A bare code: no scheme, no query, no spaces.
        if testo and not any(c in testo for c in " ?&:/'\""):
            return unquote(testo), None
        raise UrlRedirectNonValido("Nessun indirizzo eneldist://redirect nel testo incollato")

    url = trovato.group(0)
    query = parse_qs(urlsplit(url).query)
    if "error" in query:
        raise AutorizzazioneNegata(query["error"][0], query.get("error_description", [None])[0])
    if "code" not in query:
        raise UrlRedirectNonValido("L'indirizzo eneldist://redirect non contiene un code")
    return query["code"][0], query.get("state", [None])[0]


@dataclass
class OAuthTokens:
    access_token: str
    refresh_token: str
    instance_url: str
    id_token: str | None = None
    raw: dict = field(default_factory=dict)


class AuthClient:
    """Authorization link -> pasted redirect -> token exchange, and refresh."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        self._code_verifier: str | None = None
        self._code_challenge: str | None = None
        self._oauth_state: str | None = None
        # Active Salesforce host for the token endpoint. Starts as the primary
        # (custom) domain and switches to the direct one only when the primary
        # is blocked by the anti-bot check.
        self._host = SF_HOST_PRINCIPALE

    def _su_host(self, url: str) -> str:
        """Rewrite a URL of the module constants onto the active host."""
        for host in SF_HOSTS:
            prefisso = f"https://{host}"
            if url.startswith(prefisso):
                return f"https://{self._host}" + url[len(prefisso):]
        return url

    # -- Login in the user's browser ----------------------------------------

    def crea_link_autorizzazione(self) -> str:
        """URL to open in the browser. Generates the PKCE pair and the state
        once: calling it again returns the same link, so a page reload in the
        config flow does not invalidate a link the user already opened."""
        if self._code_verifier is None:
            self._code_verifier, self._code_challenge = _make_pkce_pair()
            self._oauth_state = _b64url(secrets.token_bytes(16))
        params = {
            "response_type": "code",
            "client_id": OAUTH_CLIENT_ID,
            "redirect_uri": OAUTH_REDIRECT_URI,
            "scope": OAUTH_SCOPE,
            "code_challenge": self._code_challenge,
            "code_challenge_method": "S256",
            "state": self._oauth_state,
            # No prompt=login: the portal's login page drops the OAuth request
            # after the OTP (it lands on the home page), so the link is opened
            # with a session already active and goes straight to the consent.
        }
        return f"{OAUTH_AUTHORIZE_URL}?{urlencode(params)}"

    async def async_scambia_url_redirect(self, testo_incollato: str) -> OAuthTokens:
        """Check the pasted redirect URL and exchange its code for the tokens."""
        if self._code_verifier is None:
            raise AuthError("crea_link_autorizzazione() deve precedere lo scambio del codice")
        codice, state = estrai_codice(testo_incollato)
        # A bare code has no state to compare: PKCE still ties it to this
        # verifier, so a code from another link is refused by the server.
        if state is not None and not secrets.compare_digest(state, self._oauth_state or ""):
            raise StateNonCorrispondente("Lo state dell'URL incollato non è quello di questo link")
        return await self._con_fallback_host(self._async_exchange_code, codice)

    async def _async_exchange_code(self, code: str) -> OAuthTokens:
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": self._code_verifier,
            "redirect_uri": OAUTH_REDIRECT_URI,
            "client_id": OAUTH_CLIENT_ID,
        }
        async with self._session.post(self._su_host(OAUTH_TOKEN_URL), data=data) as resp:
            payload = self._json_token(
                await _leggi_testo(resp), resp.status, "Scambio del codice OAuth fallito"
            )
        return self._tokens_from_payload(payload)

    # -- Refresh --------------------------------------------------------------

    async def async_refresh_access_token(self, refresh_token: str) -> OAuthTokens:
        """Get a fresh access_token: the path the integration uses on every
        normal start/renewal."""
        return await self._con_fallback_host(self._async_refresh_su_host, refresh_token)

    async def _async_refresh_su_host(self, refresh_token: str) -> OAuthTokens:
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": OAUTH_CLIENT_ID,
        }
        async with self._session.post(self._su_host(OAUTH_TOKEN_URL), data=data) as resp:
            payload = self._json_token(
                await _leggi_testo(resp), resp.status, "Scambio del refresh_token fallito"
            )
        # Salesforce does not always return a new refresh_token on refresh - the
        # old one is kept if a new one does not arrive.
        payload.setdefault("refresh_token", refresh_token)
        return self._tokens_from_payload(payload)

    async def _con_fallback_host(self, chiamata, argomento: str) -> OAuthTokens:
        """Try the primary host first (so it returns to the official domain as
        soon as Enel lifts the block), falling back to the direct host if the
        primary answers with the anti-bot page."""
        self._host = SF_HOST_PRINCIPALE
        try:
            return await chiamata(argomento)
        except AccessoBloccato:
            _LOGGER.warning(
                "Token endpoint bloccato su %s, riprovo via %s",
                SF_HOST_PRINCIPALE,
                SF_HOST_DIRETTO,
            )
            self._host = SF_HOST_DIRETTO
            return await chiamata(argomento)

    # -- Token endpoint responses -------------------------------------------

    @staticmethod
    def _json_token(testo: str, status: int, contesto: str) -> dict:
        """JSON of the token endpoint; an HTML page in its place is a
        ParsingError (hence an AuthError), not a generic ValueError.

        An error status is turned into the matching exception (see
        _errore_oauth): app_blocked and invalid_grant need opposite reactions,
        waiting for E-Distribuzione versus a new login."""
        try:
            payload = json.loads(testo)
        except json.JSONDecodeError as err:
            raise ParsingError(
                f"Risposta non-JSON dal token endpoint (status {status}, "
                f"{len(testo)} caratteri)"
            ) from err
        if status != 200:
            errore = payload if isinstance(payload, dict) else {}
            raise _errore_oauth(
                errore.get("error"), errore.get("error_description"), f"{contesto} ({status})"
            )
        return payload

    @staticmethod
    def _tokens_from_payload(payload: dict) -> OAuthTokens:
        try:
            return OAuthTokens(
                access_token=payload["access_token"],
                refresh_token=payload["refresh_token"],
                instance_url=payload.get("instance_url", ""),
                id_token=payload.get("id_token"),
                raw=payload,
            )
        except KeyError as err:
            raise ParsingError(f"Campo atteso mancante nella risposta del token endpoint: {err}") from err
