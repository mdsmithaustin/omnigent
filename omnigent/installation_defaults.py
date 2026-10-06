"""Default namespaces for the mdsmithaustin installation."""

from pathlib import Path
from typing import Final

USER_DIRNAME: Final = ".omnigent-mdsmithaustin"
PROJECT_CONFIG_RELPATH: Final = Path(USER_DIRNAME) / "config.yaml"
DEFAULT_LOCAL_PORT: Final = 6768
NATIVE_TMP_PREFIX: Final = "mdma"
PRIME_TMP_PREFIX: Final = "mdp"
LAUNCHD_HOST_LABEL: Final = "com.mdsmithaustin.omni.host"
SYSTEMD_HOST_UNIT: Final = "omni-mdsmithaustin-host.service"
KEYRING_SERVICE: Final = "mdsmithaustin-omni"
SESSION_COOKIE_NAME: Final = "mdsmithaustin_ap_session"
AUTH_STATE_COOKIE_NAME: Final = "mdsmithaustin_ap_auth_state"


def default_user_dir() -> Path:
    """Resolve the installation's default directory under the current HOME."""
    return Path.home() / USER_DIRNAME
