import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List
import jwt
from fastapi import Request, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from fiskr.config import SECRET_KEY
from fiskr.database import get_db, User

logger = logging.getLogger("fiskr.auth")

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_HOURS = 24

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)

# Roles empilables : la colonne User.role contient une liste separee par des
# virgules (ex: "user,reviewer"). Les anciennes valeurs mono-role restent valides.
VALID_ROLES = {"admin", "user", "reviewer"}

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
        
    return {
        "id": user.id,
        "username": user.username,
        "full_name": user.full_name,
        "role": user.role,
        "roles": parse_roles(user.role)
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

