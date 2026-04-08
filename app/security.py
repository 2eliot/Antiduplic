from passlib.context import CryptContext


password_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return password_context.verify(plain_password, hashed_password)


def hash_password(password: str) -> str:
    return password_context.hash(password)
