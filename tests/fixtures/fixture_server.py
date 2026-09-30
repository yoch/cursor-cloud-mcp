"""Point d'entrée de test équivalent au binaire, avec le transport fictif.

Les tests stdio lancent ``python -m cursor_cloud_mcp`` et posent
``CURSOR_MCP_FIXTURE=1`` dans l'environnement du sous-processus. Ce module
reste disponible pour un client qui ne sait passer qu'une commande Python.
"""

import os

from cursor_cloud_mcp.__main__ import main


def run() -> None:
    os.environ["CURSOR_MCP_FIXTURE"] = "1"
    main()


if __name__ == "__main__":
    run()
