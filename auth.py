import streamlit as st
import bcrypt

def hash_password(password: str) -> str:
    """Hash password with bcrypt. Returns decoded string."""
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(password.encode("utf-8"), salt)
    return hashed.decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Verify password against bcrypt hash."""
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


def login_user(user_row) -> None:
    """Write user fields into st.session_state.
    Sets: logged_in=True, user_id, user_name, user_email, user_role, user_team_id

    user_row is a sqlite3.Row object (accessed like a dict).
    """
    st.session_state["logged_in"] = True
    st.session_state["user_id"] = user_row["id"]
    st.session_state["user_name"] = user_row["name"]
    st.session_state["user_email"] = user_row["email"]
    st.session_state["user_role"] = user_row["role"]
    st.session_state["user_team_id"] = user_row["team_id"]


def logout_user() -> None:
    """Clear all auth keys from st.session_state."""
    import streamlit as st
    for key in ("logged_in", "user_id", "user_name", "user_email", "user_role", "user_team_id"):
        st.session_state.pop(key, None)


def get_current_user() -> dict | None:
    """Return dict of current session user or None if not logged in.
    Keys: user_id, user_name, user_email, user_role, user_team_id
    """
    import streamlit as st
    if not st.session_state.get("logged_in"):
        return None
    return {
        "user_id": st.session_state.get("user_id"),
        "user_name": st.session_state.get("user_name"),
        "user_email": st.session_state.get("user_email"),
        "user_role": st.session_state.get("user_role"),
        "user_team_id": st.session_state.get("user_team_id"),
    }
