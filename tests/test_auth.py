"""auth.py: authorization link, pasted redirect URL, token exchange and
refresh, with no real network call (only fake in-memory sessions).

auth.py itself does not depend on Home Assistant (only aiohttp and the
standard library), but the package's __init__.py does (it imports
homeassistant.config_entries). To test it in isolation (faster, and checkable
even without homeassistant installed) this file loads const.py and auth.py
DIRECTLY through importlib, bypassing __init__.py - the same trick used by
scripts/verify_login.py.
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import sys
import types
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

PKG_DIR = Path(__file__).parent.parent / "custom_components" / "edistribuzione"


def _load_auth_module():
    pkg_name = "edistribuzione_test_auth"
    if f"{pkg_name}.auth" in sys.modules:
        return sys.modules[f"{pkg_name}.auth"]

    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [str(PKG_DIR)]
    sys.modules[pkg_name] = pkg

    def _load(modname: str, filename: str):
        spec = importlib.util.spec_from_file_location(f"{pkg_name}.{modname}", PKG_DIR / filename)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"{pkg_name}.{modname}"] = mod
        spec.loader.exec_module(mod)
        return mod

    _load("const", "const.py")
    return _load("auth", "auth.py")


auth = _load_auth_module()


class _RispostaToken:
    def __init__(self, body: str, status: int = 200) -> None:
        self._body = body
        self.status = status

    async def text(self) -> str:
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False


class _SessioneACoda:
    """Returns the queued responses, one per post, and records the payloads
    sent. No get: the login no longer browses the portal."""

    def __init__(self, risposte: list) -> None:
        self._risposte = list(risposte)
        self.post_inviati: list[dict] = []

    def post(self, url, data=None, headers=None):
        self.post_inviati.append({"url": url, "data": data})
        return self._risposte.pop(0)


TOKEN_OK = '{"access_token": "acc", "refresh_token": "ref", "instance_url": "https://x"}'
PAGINA_ANTIBOT = (
    "<html><head><title>Pardon Our Interruption</title></head><body>"
    "<p>As you were browsing something about your browser made us think you were a bot.</p>"
    '<script src="/_Incapsula_Resource?SWJIYLWA=abc"></script></body></html>'
)
APP_BLOCKED = '{"error":"app_blocked","error_description":"this app is blocked by Transaction Security"}'
INVALID_GRANT = '{"error":"invalid_grant","error_description":"expired access/refresh token"}'


def _parametri_link(client) -> dict[str, str]:
    link = client.crea_link_autorizzazione()
    return {k: v[0] for k, v in parse_qs(urlsplit(link).query).items()}


def _redirect(client, code: str = "aPrx%3D%3D") -> str:
    state = _parametri_link(client)["state"]
    return f"eneldist://redirect?code={code}&state={state}"


# ---------------------------------------------------------------------------
# Authorization link
# ---------------------------------------------------------------------------


class TestLinkAutorizzazione:
    def test_link_oauth_con_pkce_s256_sul_dominio_ufficiale(self):
        client = auth.AuthClient(None)
        link = client.crea_link_autorizzazione()
        assert link.startswith(
            "https://private.e-distribuzione.it/PortaleClienti/services/oauth2/authorize?"
        )
        params = _parametri_link(client)
        assert params["response_type"] == "code"
        assert params["redirect_uri"] == "eneldist://redirect"
        assert params["code_challenge_method"] == "S256"
        atteso = (
            base64.urlsafe_b64encode(hashlib.sha256(client._code_verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        assert params["code_challenge"] == atteso

    def test_senza_prompt_login(self):
        """With prompt=login the portal's login drops the OAuth request after
        the OTP: the link must reuse the session already open in the browser."""
        assert "prompt" not in _parametri_link(auth.AuthClient(None))

    def test_il_verifier_non_finisce_nel_link(self):
        client = auth.AuthClient(None)
        assert client._code_verifier is None
        link = client.crea_link_autorizzazione()
        assert client._code_verifier not in link

    def test_stesso_link_a_ogni_chiamata(self):
        """A reload of the form must not invalidate a link already opened."""
        client = auth.AuthClient(None)
        assert client.crea_link_autorizzazione() == client.crea_link_autorizzazione()

    def test_ogni_client_ha_il_suo_state_e_verifier(self):
        a, b = auth.AuthClient(None), auth.AuthClient(None)
        assert _parametri_link(a)["state"] != _parametri_link(b)["state"]
        assert a._code_verifier != b._code_verifier


# ---------------------------------------------------------------------------
# Pasted text
# ---------------------------------------------------------------------------


class TestEstraiCodice:
    def test_url_completo(self):
        assert auth.estrai_codice("eneldist://redirect?code=abc%3D%3D&state=xyz") == (
            "abc==",
            "xyz",
        )

    def test_url_reale_con_i_parametri_della_community(self):
        """Shape of the real handleRedirect URL: percent-encoded code plus the
        sfdc_community_* parameters after the state."""
        testo = (
            "eneldist://redirect?code=aPrx.UD_W%3D&state=xyz"
            "&sfdc_community_url=https%3A%2F%2Fprivate.e-distribuzione.it%2FPortaleClienti"
            "&sfdc_community_id=0DB1o000000TRLsGAO"
        )
        assert auth.estrai_codice(testo) == ("aPrx.UD_W=", "xyz")

    def test_pezzo_del_sorgente_della_pagina(self):
        testo = "SfdcApp.projectOneNavigator.handleRedirect('eneldist://redirect?code=abc&state=xyz');"
        assert auth.estrai_codice(testo) == ("abc", "xyz")

    def test_messaggio_della_console_di_chrome(self):
        testo = (
            "Failed to launch 'eneldist://redirect?code=abc&state=xyz' because the "
            "scheme does not have a registered handler."
        )
        assert auth.estrai_codice(testo) == ("abc", "xyz")

    def test_spazi_e_virgolette_attorno(self):
        assert auth.estrai_codice('  "eneldist://redirect?code=abc&state=xyz"\n') == (
            "abc",
            "xyz",
        )

    def test_solo_il_codice(self):
        assert auth.estrai_codice("abc%3D%3D") == ("abc==", None)

    @pytest.mark.parametrize(
        "testo",
        ["", "https://private.e-distribuzione.it/qualcosa", "eneldist://redirect?state=x", "a b"],
    )
    def test_testo_senza_codice(self, testo):
        with pytest.raises(auth.UrlRedirectNonValido):
            auth.estrai_codice(testo)

    def test_redirect_con_errore_oauth(self):
        with pytest.raises(auth.AutorizzazioneNegata) as errore:
            auth.estrai_codice(
                "eneldist://redirect?error=access_denied&error_description=end-user+denied"
            )
        assert errore.value.codice == "access_denied"
        assert errore.value.descrizione == "end-user denied"


# ---------------------------------------------------------------------------
# Code exchange
# ---------------------------------------------------------------------------


class TestScambioCodice:
    async def test_scambio_riuscito_con_verifier_e_codice_decodificato(self):
        session = _SessioneACoda([_RispostaToken(TOKEN_OK)])
        client = auth.AuthClient(session)
        tokens = await client.async_scambia_url_redirect(_redirect(client))
        assert (tokens.access_token, tokens.refresh_token) == ("acc", "ref")
        inviato = session.post_inviati[0]
        assert inviato["url"].endswith("/PortaleClienti/services/oauth2/token")
        assert inviato["data"]["grant_type"] == "authorization_code"
        assert inviato["data"]["code"] == "aPrx=="
        assert inviato["data"]["code_verifier"] == client._code_verifier
        assert inviato["data"]["redirect_uri"] == "eneldist://redirect"

    async def test_state_diverso_non_viene_inviato(self):
        session = _SessioneACoda([])
        client = auth.AuthClient(session)
        client.crea_link_autorizzazione()
        with pytest.raises(auth.StateNonCorrispondente):
            await client.async_scambia_url_redirect(
                "eneldist://redirect?code=abc&state=altro"
            )
        assert session.post_inviati == []

    async def test_senza_link_non_si_scambia(self):
        with pytest.raises(auth.AuthError):
            await auth.AuthClient(_SessioneACoda([])).async_scambia_url_redirect(
                "eneldist://redirect?code=abc"
            )

    async def test_codice_scaduto_o_gia_usato(self):
        session = _SessioneACoda([_RispostaToken(INVALID_GRANT, status=400)])
        client = auth.AuthClient(session)
        with pytest.raises(auth.TokenNonValido):
            await client.async_scambia_url_redirect(_redirect(client))

    async def test_antibot_sul_dominio_ufficiale_riprova_sul_diretto(self):
        session = _SessioneACoda([_RispostaToken(PAGINA_ANTIBOT), _RispostaToken(TOKEN_OK)])
        client = auth.AuthClient(session)
        tokens = await client.async_scambia_url_redirect(_redirect(client))
        assert tokens.refresh_token == "ref"
        host_provati = [u["url"].split("/")[2] for u in session.post_inviati]
        assert host_provati == ["private.e-distribuzione.it", "edistribuzione.my.site.com"]

    async def test_app_blocked(self):
        session = _SessioneACoda([_RispostaToken(APP_BLOCKED, status=400)])
        client = auth.AuthClient(session)
        with pytest.raises(auth.AppBloccata):
            await client.async_scambia_url_redirect(_redirect(client))


# ---------------------------------------------------------------------------
# Refresh and token endpoint errors
# ---------------------------------------------------------------------------


class TestAccessoBloccato:
    def test_e_un_auth_error(self):
        assert issubclass(auth.AccessoBloccato, auth.AuthError)

    async def test_lo_script_di_imperva_in_una_pagina_normale_non_e_un_blocco(self):
        """Imperva also adds _Incapsula_Resource to normal pages: on its own it
        must not count as a block (here it ends as a non-JSON ParsingError)."""
        session = _SessioneACoda(
            [_RispostaToken('<html><script src="/_Incapsula_Resource?x=1"></script></html>')]
        )
        with pytest.raises(auth.ParsingError):
            await auth.AuthClient(session).async_refresh_access_token("rt")

    async def test_refresh_bloccato_su_entrambi_gli_host(self):
        session = _SessioneACoda([_RispostaToken(PAGINA_ANTIBOT), _RispostaToken(PAGINA_ANTIBOT)])
        with pytest.raises(auth.AccessoBloccato):
            await auth.AuthClient(session).async_refresh_access_token("rt")
        host_provati = [u["url"].split("/")[2] for u in session.post_inviati]
        assert host_provati == ["private.e-distribuzione.it", "edistribuzione.my.site.com"]

    async def test_incapsula_incident_e_un_blocco(self):
        pagina = "<html>Request unsuccessful. Incapsula incident ID: 123-456</html>"
        session = _SessioneACoda([_RispostaToken(pagina), _RispostaToken(pagina)])
        with pytest.raises(auth.AccessoBloccato):
            await auth.AuthClient(session).async_refresh_access_token("rt")

    async def test_refresh_fallback_sul_dominio_diretto(self):
        session = _SessioneACoda(
            [_RispostaToken(PAGINA_ANTIBOT), _RispostaToken('{"access_token": "nuovo"}')]
        )
        tokens = await auth.AuthClient(session).async_refresh_access_token("rt")
        assert tokens.access_token == "nuovo"
        assert session.post_inviati[-1]["url"].split("/")[2] == "edistribuzione.my.site.com"

    async def test_refresh_riuscito_tiene_il_vecchio_refresh_token(self):
        session = _SessioneACoda([_RispostaToken('{"access_token": "nuovo"}')])
        tokens = await auth.AuthClient(session).async_refresh_access_token("rt")
        assert (tokens.access_token, tokens.refresh_token) == ("nuovo", "rt")


class TestErroriToken:
    async def test_app_blocked_e_un_blocco_non_un_token_revocato(self):
        session = _SessioneACoda([_RispostaToken(APP_BLOCKED, status=400)])
        with pytest.raises(auth.AppBloccata) as errore:
            await auth.AuthClient(session).async_refresh_access_token("rt")
        assert not isinstance(errore.value, auth.TokenNonValido)
        # On the main domain (no anti-bot) the other host is not tried: it is
        # the client that is rejected, not the domain.
        assert len(session.post_inviati) == 1

    async def test_invalid_grant_e_un_token_non_valido(self):
        session = _SessioneACoda([_RispostaToken(INVALID_GRANT, status=400)])
        with pytest.raises(auth.TokenNonValido, match="expired access/refresh token"):
            await auth.AuthClient(session).async_refresh_access_token("rt")

    async def test_altri_errori_oauth_restano_generici(self):
        session = _SessioneACoda([_RispostaToken('{"error":"invalid_client"}', status=400)])
        with pytest.raises(auth.AuthError) as errore:
            await auth.AuthClient(session).async_refresh_access_token("rt")
        assert type(errore.value) is auth.AuthError
        assert "invalid_client" in str(errore.value)

    async def test_pagina_html_e_un_parsing_error_senza_il_corpo(self):
        """The body is not copied into the exception (it ends in the logs)."""
        session = _SessioneACoda([_RispostaToken("<html>manutenzione segreta</html>")])
        with pytest.raises(auth.ParsingError) as errore:
            await auth.AuthClient(session).async_refresh_access_token("rt")
        assert "segreta" not in str(errore.value)
