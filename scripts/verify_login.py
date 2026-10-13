"""Test the real login and API code (auth.py + api.py) from the terminal,
without Home Assistant in the middle.

The same path as the config flow: you log in to the portal in your own
browser, open the printed authorization link, paste back the
eneldist://redirect URL, and the script
exchanges the code and lists the account's PODs plus yesterday's consumption
of the first one.

Nothing is written to disk: the tokens live only in this process.

It imports auth.py/api.py/const.py DIRECTLY, bypassing __init__.py (which
imports homeassistant, not needed here: the three modules depend only on
aiohttp and the standard library).

Usage:
    pip install aiohttp
    python3 scripts/verify_login.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from datetime import date, timedelta
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "edistribuzione"


def _load_modules():
    """Load const.py, auth.py and api.py as an isolated mini-package, without
    running __init__.py (which requires homeassistant). Returns (auth, api)."""
    pkg_name = "edistribuzione_standalone"
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [str(PKG_DIR)]
    sys.modules[pkg_name] = pkg

    def _load(modname: str):
        spec = importlib.util.spec_from_file_location(
            f"{pkg_name}.{modname}", PKG_DIR / f"{modname}.py"
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"{pkg_name}.{modname}"] = mod
        spec.loader.exec_module(mod)
        return mod

    _load("const")
    return _load("auth"), _load("api")


async def main() -> None:
    import aiohttp

    auth, api = _load_modules()
    async with aiohttp.ClientSession() as session:
        client = auth.AuthClient(session)
        print("1. Accedi all'area clienti E-Distribuzione nel browser (password + OTP).")
        print("2. Nella stessa scheda apri questo link e premi Consenti:\n")
        print(client.crea_link_autorizzazione())
        print("\n3. Sulla pagina bianca premi F12 > Console ed esegui:")
        print(r"""   copy(document.documentElement.outerHTML.match(/eneldist:\/\/redirect\?[^'"<\s]+/)[0])""")
        print("4. Incolla qui l'indirizzo eneldist://redirect?code=... copiato.")
        incollato = input("> ")

        tokens = await client.async_scambia_url_redirect(incollato)
        print("\nAccesso riuscito: refresh_token ottenuto (non viene salvato).")

        misure = api.ApiClient(session, tokens.access_token)
        pods = await misure.async_get_supplies()
        if not pods:
            print("Nessun POD sull'account.")
            return
        for pod in pods:
            print(f"POD {pod['IdPod']} - {pod.get('PointOfMeasureMunicipality', '')}")

        ieri = date.today() - timedelta(days=1)
        giorni = await misure.async_get_monthly_load_profile(
            ieri - timedelta(days=6), ieri, pods[0]["IdPod"]
        )
        print(f"\nCurva giornaliera degli ultimi 7 giorni per {pods[0]['IdPod']} (dati grezzi):")
        for riga in giorni:
            print(riga)


if __name__ == "__main__":
    asyncio.run(main())
