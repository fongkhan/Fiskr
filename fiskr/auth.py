import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List
import jwt
from fastapi import Request, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from fiskr.config import SECRET_KEY, config
from fiskr.database import get_db, User, ApiKey

logger = logging.getLogger("fiskr.auth")

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_HOURS = 24


def security_config() -> Dict[str, Any]:
    """Section security de config.yaml avec defauts durcis."""
    sec = config.get("security", {}) or {}
    return {
        "max_login_failures": int(sec.get("max_login_failures", 5) or 5),
        "lockout_minutes": int(sec.get("lockout_minutes", 15) or 15),
        "min_password_length": int(sec.get("min_password_length", 12) or 12),
        "session_hours": int(sec.get("session_hours", 8) or 8),
        "secure_cookies": bool(sec.get("secure_cookies", False)),
        "cookie_samesite": str(sec.get("cookie_samesite", "strict") or "strict"),
    }


def validate_password(password: str) -> None:
    """
    Politique de mots de passe (comptes humains) : longueur minimale
    configurable (defaut 12) + au moins une minuscule, une majuscule et un
    chiffre. Leve ValueError avec un message destine a l'utilisateur.
    """
    pw = password or ""
    min_len = security_config()["min_password_length"]
    missing = []
    if len(pw) < min_len:
        missing.append(f"au moins {min_len} caractères")
    if not any(c.islower() for c in pw):
        missing.append("une minuscule")
    if not any(c.isupper() for c in pw):
        missing.append("une majuscule")
    if not any(c.isdigit() for c in pw):
        missing.append("un chiffre")
    if missing:
        raise ValueError("Mot de passe trop faible : il faut " + ", ".join(missing) + ".")


# ------------------ MFA TOTP (RFC 6238, stdlib uniquement) ------------------

TOTP_PERIOD = 30
TOTP_DIGITS = 6


def generate_totp_secret() -> str:
    """Secret base32 de 160 bits (RFC 4226), compatible Google Authenticator & co."""
    import base64
    import secrets as _secrets
    return base64.b32encode(_secrets.token_bytes(20)).decode("ascii")


def totp_code(secret: str, at_time: Optional[float] = None) -> str:
    """Code TOTP a 6 chiffres pour l'instant donne (HMAC-SHA1, pas de 30 s)."""
    import base64
    import hmac
    import struct
    import time as _time
    key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))
    counter = int((at_time if at_time is not None else _time.time()) // TOTP_PERIOD)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10 ** TOTP_DIGITS)).zfill(TOTP_DIGITS)


def verify_totp(secret: str, code: str, window: int = 1) -> bool:
    """
    Verifie un code en tolerant ±window pas de 30 s (derive d'horloge).
    Comparaison en temps constant.
    """
    import hmac as _hmac
    import time as _time
    candidate = (code or "").strip().replace(" ", "")
    if not secret or not candidate.isdigit() or len(candidate) != TOTP_DIGITS:
        return False
    now = _time.time()
    for step in range(-window, window + 1):
        expected = totp_code(secret, now + step * TOTP_PERIOD)
        if _hmac.compare_digest(expected, candidate):
            return True
    return False


def totp_provisioning_uri(secret: str, username: str) -> str:
    """URI otpauth:// a saisir/scanner dans l'application d'authentification."""
    from urllib.parse import quote
    label = quote(f"Fiskr:{username}")
    return (f"otpauth://totp/{label}?secret={secret}&issuer=Fiskr"
            f"&algorithm=SHA1&digits={TOTP_DIGITS}&period={TOTP_PERIOD}")


# ------------------ CLES D'API TECHNIQUES (comptes de service) ------------------

API_KEY_PREFIX_LEN = 12  # « fsk_ » + 8 caracteres d'identification


def hash_api_key(full_key: str) -> str:
    return hashlib.sha256(full_key.encode("utf-8")).hexdigest()


def authenticate_api_key(db: Session, full_key: str) -> Optional[Dict[str, Any]]:
    """
    Authentifie une cle « fsk_... » : lookup par prefixe, verification du hash
    SHA-256, cle non revoquee. Retourne un profil de compte de service ou None.
    """
    if not full_key or not full_key.startswith("fsk_") or len(full_key) < API_KEY_PREFIX_LEN:
        return None
    key = db.query(ApiKey).filter(ApiKey.prefix == full_key[:API_KEY_PREFIX_LEN]).first()
    if key is None or key.revoked_at is not None:
        return None
    if key.key_hash != hash_api_key(full_key):
        return None
    key.last_used_at = datetime.utcnow()
    db.commit()
    return {
        "id": -key.id,  # negatif : jamais confondu avec un utilisateur humain
        "username": f"apikey:{key.name}",
        "full_name": f"Clé d'API « {key.name} »",
        "role": key.roles or "user",
        "roles": parse_roles(key.roles or "user"),
        "is_api_key": True,
    }

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)

# Lecture seule auditeur : toute methode mutante est refusee, a l'exception
# de la gestion de sa propre session (deconnexion, mot de passe, MFA)
_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_AUDITOR_WRITE_WHITELIST = {
    "/api/auth/logout", "/api/users/me/password",
    "/api/auth/totp/setup", "/api/auth/totp/confirm", "/api/auth/totp/disable",
}


def enforce_auditor_readonly(request: Optional[Request], roles: List[str]) -> None:
    """403 pour un auditeur sur toute ecriture (hors gestion de sa session)."""
    if "auditor" not in roles or request is None:
        return
    if request.method in _MUTATING_METHODS and request.url.path not in _AUDITOR_WRITE_WHITELIST:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Compte auditeur : accès en lecture seule."
        )

# Roles empilables : la colonne User.role contient une liste separee par des
# virgules (ex: "user,reviewer"). Les anciennes valeurs mono-role restent valides.
# `param` : equipe criblage (parametrage) — blocking keys et regles anti-faux
# positifs, criblage comme filtrage. Les admins ont toujours ces droits.
# `auditor` : lecture seule integrale (controleur externe) — exclusif, jamais
# combine a un autre role, toute ecriture refusee par get_current_user.
VALID_ROLES = {"admin", "user", "reviewer", "blocking", "rules", "auditor"}

def parse_roles(role_str: Optional[str]) -> List[str]:
    """Decoupe la chaine de roles en liste normalisee (minuscules, sans doublons)."""
    seen = []
    for token in (role_str or "").split(","):
        role = token.strip().lower()
        if role and role not in seen:
            seen.append(role)
    return seen

def normalize_roles(role_str: str) -> str:
    """Forme canonique stockee en base : roles valides, dedupliques, tries."""
    roles = parse_roles(role_str)
    invalid = [r for r in roles if r not in VALID_ROLES]
    if not roles or invalid:
        raise ValueError(
            f"Rôle(s) invalide(s): {', '.join(invalid) if invalid else '(vide)'}. "
            f"Rôles autorisés: {', '.join(sorted(VALID_ROLES))}."
        )
    if "auditor" in roles and len(roles) > 1:
        raise ValueError("Le rôle auditeur est exclusif : il ne se combine à aucun autre rôle (lecture seule).")
    return ",".join(sorted(roles))

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Creates a signed JWT access token."""
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS)
    to_encode.update({"exp": expire, "iat": datetime.now(timezone.utc)})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

def decode_access_token(token: str) -> Optional[Dict[str, Any]]:
    """Decodes and validates a JWT token."""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except jwt.PyJWTError:
        return None

async def get_current_user(
    request: Request,
    bearer_token: Optional[str] = Depends(oauth2_scheme),
    db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """
    FastAPI dependency enforcing authentication via Cookie or Authorization Bearer header.
    Returns current user info dictionary or raises HTTP 401 Unauthorized.
    """
    token = bearer_token

    # Comptes de service : cle d'API « fsk_... » via X-API-Key ou Bearer
    api_key_candidate = None
    if request is not None:
        api_key_candidate = request.headers.get("X-API-Key")
    if not api_key_candidate and token and token.startswith("fsk_"):
        api_key_candidate = token
    if api_key_candidate:
        service_account = authenticate_api_key(db, api_key_candidate)
        if service_account:
            enforce_auditor_readonly(request, service_account["roles"])
            return service_account
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Clé d'API invalide ou révoquée.",
        )

    # Fallback to cookie if Bearer token is not in header
    if not token and request:
        token = request.cookies.get("fiskr_access_token")

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Non authentifié. Veuillez vous connecter.",
            headers={"WWW-Authenticate": "Bearer"},
        )
        
    payload = decode_access_token(token)
    if not payload or "sub" not in payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Jeton d'authentification invalide ou expiré.",
            headers={"WWW-Authenticate": "Bearer"},
        )
        
    username = payload["sub"]
    user = db.query(User).filter(User.username == username).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Utilisateur introuvable.",
            headers={"WWW-Authenticate": "Bearer"},
        )
        
    roles = parse_roles(user.role)
    enforce_auditor_readonly(request, roles)
    return {
        "id": user.id,
        "username": user.username,
        "full_name": user.full_name,
        "role": user.role,
        "roles": roles,
        "totp_enabled": bool(user.totp_enabled),
    }

def require_roles(*allowed: str):
    """
    Fabrique une dependance FastAPI exigeant l'un des roles donnes.
    Le role 'admin' passe toujours, quel que soit le role exige.
    """
    async def dependency(
        current_user: Dict[str, Any] = Depends(get_current_user)
    ) -> Dict[str, Any]:
        roles = set(parse_roles(current_user.get("role")))
        if "admin" in roles or roles & set(allowed):
            return current_user
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Accès refusé. Rôle requis: " + " ou ".join(allowed) + "."
        )
    return dependency

async def require_admin(
    current_user: Dict[str, Any] = Depends(get_current_user)
) -> Dict[str, Any]:
    """
    FastAPI dependency enforcing administrator privileges ('admin' role).
    Raises HTTP 403 Forbidden if current user is not an admin.
    """
    if "admin" not in parse_roles(current_user.get("role")):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Accès refusé. Privilèges d'administrateur requis."
        )
    return current_user

# Validation humaine des snapshots en homologation (reviewer ou admin)
require_reviewer = require_roles("reviewer")

# Parametrage des blocking keys (role dedie 'blocking', ou admin)
require_blocking = require_roles("blocking")

# Gestion des regles anti-faux positifs (equipe criblage via le role 'rules', ou admin)
require_fprules = require_roles("rules")

