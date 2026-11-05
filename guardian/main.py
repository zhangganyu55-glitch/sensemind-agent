"""Local app: `uvicorn guardian.main:app`.

The app is created lazily so importing `create_app` from this module (as tests
do) does not open or migrate the real `guardian.db` as an import side effect.
Uvicorn accesses the `app` attribute, which triggers creation.
"""
from .factory import create_app


def __getattr__(name):
    if name == "app":
        return create_app()
    raise AttributeError(name)
