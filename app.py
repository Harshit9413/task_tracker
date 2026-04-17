import re
import secrets
from pathlib import Path

from datetime import date, timedelta
from dotenv import load_dotenv
import streamlit as st
from langchain_core.messages import HumanMessage, AIMessage
from chatbot import run_chatbot_query
from streamlit_quill import st_quill



from database import (
    init_db,
    get_all_teams,
    get_team_by_id,
    get_user_by_email,
    create_user,
    get_updates_by_user,
    get_update_today,
    create_update,
    edit_update,
    get_users_by_team,
    get_team_updates_by_date,
    get_missing_users_today,
    upsert_meeting_notes,
    get_meeting_notes,
    get_all_users_with_teams,
    get_all_teams_updates_by_date,
    create_session,
    get_session_user,
    delete_session,
    create_team,
    update_team_name,
    get_team_leader,
)
from auth import (
    hash_password,
    verify_password,
    login_user,
    logout_user,
    get_current_user,
)

load_dotenv(Path(__file__).parent / ".env")
st.set_page_config(page_title="Team Update Tracker", layout="wide")
init_db()

# ---------------------------------------------------------------------------
# SQLite-backed session helpers
# Token lives in st.query_params["t"] (survives refresh) and in DB (survives restart).
# ---------------------------------------------------------------------------


def _save_session(user_id: int) -> str:
    """Create a DB session for user_id, put token in URL, return token."""
    token = secrets.token_urlsafe(16)
    create_session(token, user_id, days=7)
    st.query_params["t"] = token
    return token


def _restore_session() -> bool:
    """If a valid token is in the URL, look it up in DB and restore session_state."""
    token = st.query_params.get("t")
    if not token:
        return False
    user = get_session_user(token)
    if not user:
        st.query_params.clear()
        return False
    st.session_state["logged_in"] = True
    st.session_state["user_id"] = user["id"]
    st.session_state["user_name"] = user["name"]
    st.session_state["user_email"] = user["email"]
    st.session_state["user_role"] = user["role"]
    st.session_state["user_team_id"] = user["team_id"]
    return True


def _clear_session() -> None:
    """Delete DB session and clear URL token."""
    token = st.query_params.get("t")
    if token:
        delete_session(token)
    st.query_params.clear()


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def is_empty_quill(content: str | None) -> bool:
    """Return True if Quill editor returned nothing meaningful."""
    if not content:
        return True
    return not re.sub(r"<[^>]+>", "", content).strip()


# ---------------------------------------------------------------------------
# Page: Login / Register
# ---------------------------------------------------------------------------


def show_guide():
    st.title("How to Use Team Update Tracker")
    st.caption("A complete walkthrough of every feature in the portal.")

    # ── Demo credentials ──────────────────────────────────────────────────────
    st.divider()
    st.subheader("Demo Login Credentials")
    st.info("All demo accounts use the password: **password123**")

    col1, col2, col3 = st.columns(3)
    with col1:
        st.markdown("**Member**")
        st.code("bob@example.com\npassword123", language="text")
        st.caption("Team: Alpha")
        st.code("carol@example.com\npassword123", language="text")
        st.caption("Team: Alpha")
    with col2:
        st.markdown("**Team Leader**")
        st.code("alice@example.com\npassword123", language="text")
        st.caption("Team: Alpha")
        st.code("dave@example.com\npassword123", language="text")
        st.caption("Team: Beta")
    with col3:
        st.markdown("**Manager**")
        st.code("frank@example.com\npassword123", language="text")
        st.caption("Sees all teams")

    # ── Roles overview ────────────────────────────────────────────────────────
    st.divider()
    st.subheader("Who Can Do What?")

    r1, r2, r3 = st.columns(3)
    with r1:
        st.markdown("""
**Member**
- Submit daily update
- View & edit own past updates
- See team name & leader name
""")
    with r2:
        st.markdown("""
**Team Leader**
- Everything a member can do
- View entire team's updates
- See who is missing
- Write meeting notes (MoM)
- Use AI chatbot
- Edit team name
""")
    with r3:
        st.markdown("""
**Manager**
- View all teams at once
- See every member's updates
- No submit / chatbot access
""")

    # ── Step-by-step sections ─────────────────────────────────────────────────
    st.divider()
    st.subheader("Step-by-Step Guide")

    # 1 — Login / Register
    with st.expander("1   Login & Register", expanded=True):
        st.markdown("""
**Login**
1. Enter your email and password on the **Login** tab.
2. Click **Login** — you will be taken to your dashboard.
3. Your session is saved in the URL. If you refresh the page you stay logged in.

**Register as a Member**
1. Go to the **Register** tab.
2. Fill in your name, email, and password.
3. Choose role **Member**.
4. Pick the team you want to join from the dropdown.
5. Click **Register** and then log in.

**Register as a Team Leader**
1. Go to the **Register** tab.
2. Fill in your details and choose role **Leader**.
3. Enter a **new team name** — this creates your team on the spot.
4. Click **Register** and then log in.
""")

    # 2 — Add Update
    with st.expander("2   Add Update  (Member & Leader)"):
        st.markdown("""
1. Click **Add Update** in the left sidebar.
2. Write your daily update in the rich-text editor — supports **bold**, *italic*, bullet lists, numbered lists, tables, and more.
3. You can submit only once per day. If you already submitted, the editor will be pre-filled so you can **edit** it.
4. Click **Submit Update** to save.

**Tips**
- Use bullet points for clarity.
- Mention blockers separately so the leader notices them.
""")

    # 3 — My Updates
    with st.expander("3   My Updates  (Member & Leader)"):
        st.markdown("""
1. Click **My Updates** in the sidebar.
2. Choose how many past days to view (1–30) using the slider.
3. Your updates are listed newest-first, with the date and full content.
4. Click **Edit** on any entry to update it.
""")

    # 4 — Team View (Leader only)
    with st.expander("4   Team View  (Leader only)"):
        st.markdown("""
1. Click **Team View** in the sidebar.
2. Pick a date using the date picker (defaults to today).
3. You will see two sections:
   - **Submitted** — members who sent their update, with full content shown.
   - **Missing** — members who have not submitted yet.
4. Use this before the daily standup to know who is ready.
""")

    # 5 — Meeting Notes
    with st.expander("5   Meeting Notes / MoM  (Leader only)"):
        st.markdown("""
1. Click **Meeting Notes** in the sidebar.
2. Select the date for the meeting.
3. Write the Minutes of Meeting (MoM) in the rich-text editor.
4. Click **Save Notes** — notes are stored per team per date.
5. The chatbot can fetch these notes and include them in email reports.
""")

    # 6 — Chatbot
    with st.expander("6   AI Chatbot  (Leader only)"):
        st.markdown("""
The chatbot knows who you are and which team you belong to. It can answer questions and take actions.

**Example questions you can ask:**
- *"Who are my team members?"*
- *"Give me today's updates"*
- *"Who hasn't submitted today?"*
- *"Show me yesterday's updates"*
- *"Send today's team updates to the team email"*
- *"What were the meeting notes for today?"*

**Rules:**
- The chatbot only shows data for your own team.
- It will not summarise or shorten member updates — they appear word-for-word.
- Asking for tomorrow's updates will be politely refused (future dates don't exist).
- Click **New Chat** (top-right) to clear the conversation and start fresh.
""")

    # 7 — Team Settings
    with st.expander("7   Team Settings  (Leader only)"):
        st.markdown("""
1. Click **Team Settings** in the sidebar.
2. You can rename your team — type the new name and click **Update Name**.
3. The full member roster is listed below with role badges (Leader / Member).
4. A leader can only manage the team they created — there is no option to create a second team.
""")

    # 8 — All Teams (Manager)
    with st.expander("8   All Teams  (Manager only)"):
        st.markdown("""
1. Log in as a Manager.
2. Click **All Teams** in the sidebar.
3. Pick a date — all teams and all their members' updates for that date are shown in one view.
4. Members with no update that day are listed under **Missing**.
""")

    st.divider()
    st.success(
        "You are ready to use the portal. Go to the **Login** or **Register** tab to get started."
    )


def show_login_register():
    st.title("Team Update Tracker")
    st.subheader("Please log in or register to continue")

    tab_login, tab_register, tab_guide = st.tabs(["Login", "Register", "Guide"])

    with tab_login:
        st.subheader("Login")
        email = st.text_input("Email", key="login_email")
        password = st.text_input("Password", type="password", key="login_password")

        if st.button("Login", use_container_width=True, key="login_btn"):
            if not email or not password:
                st.error("Please enter both email and password.")
            else:
                user = get_user_by_email(email.strip())
                if user and verify_password(password, user["password_hash"]):
                    login_user(user)
                    _save_session(user["id"])
                    st.rerun()
                else:
                    st.error("Invalid email or password.")

    with tab_register:
        st.subheader("Register")
        name = st.text_input("Name", key="reg_name")
        reg_email = st.text_input("Email", key="reg_email")
        reg_password = st.text_input("Password", type="password", key="reg_password")
        reg_confirm = st.text_input(
            "Confirm Password", type="password", key="reg_confirm"
        )

        reg_role = st.radio(
            "Register as",
            ["Member", "Team Leader"],
            key="reg_role",
            horizontal=True,
        )

        teams = get_all_teams()
        selected_team_id = None
        new_team_name = None

        if reg_role == "Member":
            if teams:
                team_options = {t["name"]: t["id"] for t in teams}
                selected_team_name = st.selectbox(
                    "Select your team", list(team_options.keys()), key="reg_team"
                )
                selected_team_id = team_options[selected_team_name]
            else:
                st.warning("No teams available yet. Ask a Team Leader to create one.")
        else:
            new_team_name = st.text_input(
                "Team name (you will lead this team)", key="reg_team_name"
            )
            st.caption("You can edit the team name later from Team Settings.")

        if st.button("Register", use_container_width=True, key="register_btn"):
            if not name or not reg_email or not reg_password or not reg_confirm:
                st.error("All fields are required.")
            elif reg_password != reg_confirm:
                st.error("Passwords do not match.")
            elif reg_role == "Member" and not selected_team_id:
                st.error("Please select a team.")
            elif reg_role == "Team Leader" and not new_team_name:
                st.error("Please enter a team name.")
            else:
                try:
                    pw_hash = hash_password(reg_password)
                    role = "member" if reg_role == "Member" else "leader"

                    if role == "leader":
                        # Create the team first, then assign the leader to it
                        team_id = create_team(new_team_name)
                    else:
                        team_id = selected_team_id

                    create_user(
                        name=name.strip(),
                        email=reg_email.strip().lower(),
                        password_hash=pw_hash,
                        role=role,
                        team_id=team_id,
                    )
                    st.success(
                        "Account created successfully! Please switch to the Login tab to sign in."
                    )
                except Exception as exc:
                    if "UNIQUE" in str(exc).upper():
                        st.error("An account with that email already exists.")
                    else:
                        st.error(f"Registration failed: {exc}")

    with tab_guide:
        show_guide()


# ---------------------------------------------------------------------------
# Page: Add/Edit Today's Update
# ---------------------------------------------------------------------------


def show_add_update():
    st.header("Today's Update")
    user = get_current_user()
    today_str = str(date.today())
    existing = get_update_today(user["user_id"], today_str)

    if "editing_today" not in st.session_state:
        st.session_state.editing_today = False

    if existing is None:
        # No update yet — show the editor
        st.info("You haven't submitted an update for today yet.")
        new_content = st_quill(
            value="",
            placeholder="Write your update...",
            html=True,
            key="add_update_quill",
        )
        if st.button(
            "Submit Update", use_container_width=True, key="submit_update_btn"
        ):
            if is_empty_quill(new_content):
                st.error("Update cannot be empty.")
            else:
                try:
                    create_update(user["user_id"], new_content, today_str)
                    st.success("Update submitted successfully!")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Failed to submit update: {exc}")
    else:
        # Update already submitted
        if not st.session_state.editing_today:
            st.success("Update already submitted for today.")
            st.markdown(existing["content"], unsafe_allow_html=True)
            st.caption(
                f"Created: {existing['created_at']}  |  Updated: {existing['updated_at']}"
            )
            if st.button("Edit Today's Update", key="edit_today_btn"):
                st.session_state.editing_today = True
                st.rerun()
        else:
            st.subheader("Edit Today's Update")
            edited_content = st_quill(
                value=existing["content"],
                placeholder="Write your update...",
                html=True,
                key="edit_today_quill",
            )
            col1, col2 = st.columns(2)
            with col1:
                if st.button("Save", use_container_width=True, key="save_today_btn"):
                    if is_empty_quill(edited_content):
                        st.error("Update cannot be empty.")
                    else:
                        edit_update(existing["id"], edited_content)
                        st.session_state.editing_today = False
                        st.success("Update saved.")
                        st.rerun()
            with col2:
                if st.button(
                    "Cancel", use_container_width=True, key="cancel_today_btn"
                ):
                    st.session_state.editing_today = False
                    st.rerun()


# ---------------------------------------------------------------------------
# Page: My Updates
# ---------------------------------------------------------------------------


def _render_update_card(update, label: str):
    """Render a single update card with edit support."""
    today_str = str(date.today())
    is_today  = str(update["date"]) == today_str

    st.markdown(
        f"<div style='background:#f8f9fb;border:1px solid #e2e8f0;border-radius:10px;"
        f"padding:16px 18px;min-height:180px;'>"
        f"<p style='margin:0 0 8px;font-size:13px;font-weight:600;color:#6366f1;'>{label}</p>"
        f"<p style='margin:0 0 10px;font-size:12px;color:#94a3b8;'>{update['date']}</p>",
        unsafe_allow_html=True,
    )
    if st.session_state.editing_update_id == update["id"]:
        edited = st_quill(
            value=update["content"],
            placeholder="Write your update...",
            html=True,
            key=f"edit_quill_{update['id']}",
        )
        c1, c2 = st.columns(2)
        with c1:
            if st.button("Save", use_container_width=True, key=f"save_{update['id']}"):
                if is_empty_quill(edited):
                    st.error("Update cannot be empty.")
                else:
                    edit_update(update["id"], edited)
                    st.session_state.editing_update_id = None
                    st.success("Saved.")
                    st.rerun()
        with c2:
            if st.button("Cancel", use_container_width=True, key=f"cancel_{update['id']}"):
                st.session_state.editing_update_id = None
                st.rerun()
    else:
        st.markdown(update["content"], unsafe_allow_html=True)
        st.caption(f"Updated: {update['updated_at']}")
        if is_today:
            if st.button("Edit", key=f"edit_btn_{update['id']}"):
                st.session_state.editing_update_id = update["id"]
                st.rerun()
    st.markdown("</div>", unsafe_allow_html=True)


def show_my_updates():
    st.header("My Updates")
    user = get_current_user()

    if "editing_update_id" not in st.session_state:
        st.session_state.editing_update_id = None

    updates = get_updates_by_user(user["user_id"])

    if not updates:
        st.info("No updates yet.")
        return

    today_str     = str(date.today())
    yesterday_str = str(date.today() - timedelta(days=1))

    today_up     = next((u for u in updates if str(u["date"]) == today_str), None)
    yesterday_up = next((u for u in updates if str(u["date"]) == yesterday_str), None)
    older        = [u for u in updates if str(u["date"]) not in (today_str, yesterday_str)]

    # ── Single white card: Yesterday on top, Today below ──────────────────────
    def _strip(html: str) -> str:
        h = re.sub(r'<br\s*/?>', '\n', html)
        h = re.sub(r'</p>|</li>|</div>', '\n', h)
        h = re.sub(r'<[^>]+>', '', h)
        import html as _html_mod
        return re.sub(r'\n{3,}', '\n\n', _html_mod.unescape(h).replace('\xa0', ' ')).strip()

    if yesterday_up:
        plain_y = _strip(yesterday_up["content"])
        yesterday_html = f"<p style='white-space:pre-wrap;font-size:14px;color:#000000;margin:0;'>{plain_y}</p>"
    else:
        yesterday_html = "<p style='color:#94a3b8;font-size:13px;margin:0;'>No update submitted yesterday.</p>"

    if today_up:
        plain_t = _strip(today_up["content"])
        today_html = f"<p style='white-space:pre-wrap;font-size:14px;color:#000000;margin:0;'>{plain_t}</p>"
    else:
        today_html = "<p style='color:#94a3b8;font-size:13px;margin:0;'>No update submitted today.</p>"

    card_html = (
"<div style='background:#ffffff;border:1px solid #e2e8f0;border-radius:12px;"
"padding:24px 28px;box-shadow:0 2px 8px rgba(0,0,0,0.06);'>"
"<p style='margin:0 0 2px;font-size:11px;font-weight:700;color:#6366f1;"
"text-transform:uppercase;letter-spacing:1px;'>Yesterday</p>"
f"<p style='margin:0 0 8px;font-size:12px;color:#94a3b8;'>{yesterday_str}</p>"
f"{yesterday_html}"
"<hr style='border:none;border-top:1px solid #e5e7eb;margin:18px 0;'/>"
"<p style='margin:0 0 2px;font-size:11px;font-weight:700;color:#10b981;"
"text-transform:uppercase;letter-spacing:1px;'>Today</p>"
f"<p style='margin:0 0 8px;font-size:12px;color:#94a3b8;'>{today_str}</p>"
f"{today_html}"
"</div>"
    )
    st.markdown(card_html, unsafe_allow_html=True)

    # Edit button for today's update (outside the static HTML block)
    if today_up:
        st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
        if st.session_state.editing_update_id == today_up["id"]:
            edited = st_quill(
                value=today_up["content"],
                placeholder="Write your update...",
                html=True,
                key=f"edit_quill_{today_up['id']}",
            )
            c1, c2 = st.columns(2)
            with c1:
                if st.button("Save", use_container_width=True, key=f"save_{today_up['id']}"):
                    if is_empty_quill(edited):
                        st.error("Update cannot be empty.")
                    else:
                        edit_update(today_up["id"], edited)
                        st.session_state.editing_update_id = None
                        st.success("Saved.")
                        st.rerun()
            with c2:
                if st.button("Cancel", use_container_width=True, key=f"cancel_{today_up['id']}"):
                    st.session_state.editing_update_id = None
                    st.rerun()
        else:
            if st.button("Edit Today's Update", key=f"edit_btn_{today_up['id']}"):
                st.session_state.editing_update_id = today_up["id"]
                st.rerun()

    # ── Older history ──────────────────────────────────────────────────────────
    if older:
        st.markdown("---")
        st.markdown("#### Update History")
        for update in older:
            with st.expander(f"{update['date']}"):
                st.markdown(update["content"], unsafe_allow_html=True)
                st.caption(f"Updated: {update['updated_at']}")


# ---------------------------------------------------------------------------
# Page: Team View (Leader only)
# ---------------------------------------------------------------------------


def show_team_view():
    st.header("Team View")
    user = get_current_user()

    selected_date = st.date_input("Date", value=date.today(), key="team_view_date")
    date_str = str(selected_date)

    members = get_users_by_team(user["user_team_id"])
    updates = get_team_updates_by_date(user["user_team_id"], date_str)
    updates_by_user = {u["user_id"]: u for u in updates}

    missing_list = get_missing_users_today(user["user_team_id"], date_str)

    total = len(members)
    submitted = total - len(missing_list)
    missing_count = len(missing_list)

    # Summary metrics
    col1, col2, col3 = st.columns(3)
    col1.metric("Members", total)
    col2.metric("Submitted", submitted)
    col3.metric("Missing", missing_count)

    st.divider()

    submitted_members = [m for m in members if m["id"] in updates_by_user]
    missing_members = [m for m in members if m["id"] not in updates_by_user]

    if submitted_members:
        st.subheader("Submitted")
        for member in submitted_members:
            with st.expander(member["name"]):
                upd = updates_by_user[member["id"]]
                st.markdown(upd["content"], unsafe_allow_html=True)
                st.caption(
                    f"Created: {upd['created_at']}  |  Updated: {upd['updated_at']}"
                )
    else:
        st.info("No updates submitted yet.")

    if missing_members:
        st.subheader("Missing")
        for member in missing_members:
            st.warning(f"{member['name']} — No update submitted")


# ---------------------------------------------------------------------------
# Page: Meeting Notes (Leader only)
# ---------------------------------------------------------------------------


def show_meeting_notes():
    st.header("Meeting Notes (MoM)")
    user = get_current_user()
    today_str = str(date.today())

    selected_date = st.date_input("Date", value=date.today(), key="mom_date")
    date_str = str(selected_date)
    is_today = date_str == today_str

    existing_notes = get_meeting_notes(user["user_team_id"], date_str)

    # Reset editing state when date changes
    if st.session_state.get("mom_last_date") != date_str:
        st.session_state.editing_mom = False
        st.session_state.mom_last_date = date_str

    if "editing_mom" not in st.session_state:
        st.session_state.editing_mom = False

    st.divider()

    if existing_notes and not st.session_state.editing_mom:
        st.success(f"Meeting notes saved for {date_str}.")
        st.markdown(existing_notes["content"], unsafe_allow_html=True)
        st.caption(
            f"Created: {existing_notes['created_at']}  |  Updated: {existing_notes['updated_at']}"
        )
        if st.button("Edit Notes", key="edit_mom_btn"):
            st.session_state.editing_mom = True
            st.rerun()
    else:
        if existing_notes:
            st.subheader("Edit Meeting Notes")
            initial_value = existing_notes["content"]
        else:
            label = "today" if is_today else date_str
            st.info(f"No meeting notes for {label} yet. Add them below.")
            initial_value = ""

        notes_content = st_quill(
            value=initial_value,
            placeholder="Write your update...",
            html=True,
            key=f"mom_quill_{date_str}",
        )

        col1, col2 = st.columns(2)
        with col1:
            if st.button("Save Notes", use_container_width=True, key="save_mom_btn"):
                if is_empty_quill(notes_content):
                    st.error("Notes cannot be empty.")
                else:
                    upsert_meeting_notes(
                        user["user_team_id"],
                        date_str,
                        notes_content,
                        user["user_id"],
                    )
                    st.session_state.editing_mom = False
                    st.success("Meeting notes saved.")
                    st.rerun()
        with col2:
            if existing_notes:
                if st.button("Cancel", use_container_width=True, key="cancel_mom_btn"):
                    st.session_state.editing_mom = False
                    st.rerun()


# ---------------------------------------------------------------------------
# Page: Chatbot (Leader only)
# ---------------------------------------------------------------------------


def show_chatbot():
    col1, col2 = st.columns([6, 1])
    col1.header("Team Update Assistant")
    if col2.button("New Chat", use_container_width=True):
        st.session_state.chat_history = []
        st.rerun()

    if "chat_history" not in st.session_state:
        st.session_state.chat_history = []

    # Display past messages
    for msg in st.session_state.chat_history:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    # Input
    if prompt := st.chat_input(
        "Ask about updates, missing submissions, or send email reports..."
    ):
        st.session_state.chat_history.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)

        with st.chat_message("assistant"):
            with st.spinner("Thinking..."):
                # Convert history to LangChain BaseMessage list (exclude current msg)
                lc_history = []
                for msg in st.session_state.chat_history[:-1]:
                    if msg["role"] == "user":
                        lc_history.append(HumanMessage(content=msg["content"]))
                    else:
                        lc_history.append(AIMessage(content=msg["content"]))

                team_id = st.session_state.get("user_team_id")
                team_name = None
                if team_id:
                    t = get_team_by_id(team_id)
                    if t:
                        team_name = t["name"]
                user_info = {
                    "name": st.session_state.get("user_name"),
                    "email": st.session_state.get("user_email"),
                    "role": st.session_state.get("user_role"),
                    "team_id": team_id,
                    "team_name": team_name,
                }
                response = run_chatbot_query(prompt, lc_history, user_info=user_info)
                st.markdown(response)

        st.session_state.chat_history.append({"role": "assistant", "content": response})


# ---------------------------------------------------------------------------
# Page: All Teams (Manager only)
# ---------------------------------------------------------------------------


def show_all_teams():
    st.header("All Teams Overview")
    selected_date = st.date_input("Date", value=date.today(), key="all_teams_date")
    date_str = str(selected_date)

    teams = get_all_teams()

    for team in teams:
        with st.expander(f"Team: {team['name']}"):
            members = get_users_by_team(team["id"])
            updates = get_team_updates_by_date(team["id"], date_str)
            updates_by_user = {u["user_id"]: u for u in updates}

            missing_list = get_missing_users_today(team["id"], date_str)

            submitted_count = len(members) - len(missing_list)
            missing_count = len(missing_list)

            # Team-level metrics
            m_col1, m_col2, m_col3 = st.columns(3)
            m_col1.metric("Members", len(members))
            m_col2.metric("Submitted", submitted_count)
            m_col3.metric("Missing", missing_count)

            st.divider()

            for member in members:
                role_badge = f"`{member['role'].capitalize()}`"
                with st.container():
                    st.markdown(f"**{member['name']}** {role_badge}")
                    if member["id"] in updates_by_user:
                        upd = updates_by_user[member["id"]]
                        st.markdown(upd["content"], unsafe_allow_html=True)
                        st.caption(
                            f"Created: {upd['created_at']}  |  Updated: {upd['updated_at']}"
                        )
                    else:
                        st.warning("Not submitted")
                    st.markdown("---")


# ---------------------------------------------------------------------------
# Main routing
# ---------------------------------------------------------------------------


def show_team_settings():
    """Leader-only: view team info and edit team name (one team per leader)."""
    st.header("Team Settings")
    user = get_current_user()
    team_id = user["user_team_id"]

    if not team_id:
        st.error("You are not assigned to any team.")
        return

    team = get_team_by_id(team_id)
    leader = get_team_leader(team_id)
    members = get_users_by_team(team_id)

    col1, col2 = st.columns(2)
    with col1:
        st.metric("Team", team["name"])
    with col2:
        st.metric("Members", len(members))

    st.divider()
    st.subheader("Edit Team Name")
    new_name = st.text_input("Team name", value=team["name"], key="team_name_edit")
    if st.button("Save Team Name", use_container_width=True):
        if not new_name.strip():
            st.error("Team name cannot be empty.")
        elif new_name.strip() == team["name"]:
            st.info("No change.")
        else:
            update_team_name(team_id, new_name.strip())
            st.success(f"Team renamed to **{new_name.strip()}**.")
            st.rerun()

    st.divider()
    st.subheader("Team Members")
    for m in members:
        role_badge = "👑 Leader" if m["role"] == "leader" else "👤 Member"
        st.write(f"{role_badge} — **{m['name']}** ({m['email']})")


def main():
    # Try to restore session from URL token on page refresh
    if not st.session_state.get("logged_in"):
        _restore_session()

    user = get_current_user()

    if not user:
        show_login_register()
        return

    role = user["user_role"]

    with st.sidebar:
        st.title("Team Update Tracker")
        st.divider()
        st.write(f"**{user['user_name']}**")
        st.caption(f"Role: {role.capitalize()}")

        # Show team + leader info for members and leaders
        team_id = user.get("user_team_id")
        if team_id:
            team = get_team_by_id(team_id)
            leader = get_team_leader(team_id)
            if team:
                st.caption(f"Team: {team['name']}")
            if leader and leader["id"] != user["user_id"]:
                st.caption(f"Leader: {leader['name']}")

        st.divider()

        if role == "member":
            pages = ["Add Update", "My Updates"]
        elif role == "leader":
            pages = [
                "Add Update",
                "My Updates",
                "Team View",
                "Meeting Notes",
                "Chatbot",
                "Team Settings",
            ]
        elif role == "manager":
            pages = ["All Teams", "My Updates"]
        else:
            pages = ["My Updates"]

        page = st.radio("Navigation", pages, label_visibility="collapsed")
        st.divider()
        if st.button("Logout", use_container_width=True):
            _clear_session()
            logout_user()
            st.rerun()

    # Route to page
    if page == "Add Update":
        show_add_update()
    elif page == "My Updates":
        show_my_updates()
    elif page == "Team View":
        show_team_view()
    elif page == "Meeting Notes":
        show_meeting_notes()
    elif page == "Chatbot":
        show_chatbot()
    elif page == "Team Settings":
        show_team_settings()
    elif page == "All Teams":
        show_all_teams()


if __name__ == "__main__":
    main()
