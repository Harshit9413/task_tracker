import sqlite3
from contextlib import asynccontextmanager
from datetime import date

from pydantic import BaseModel
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from auth import hash_password, verify_password
from database import (
    init_db,
    # Teams
    get_all_teams,
    get_team_by_id,
    # Users
    create_user,
    get_user_by_email,
    get_users_by_team, 
    get_leaders,
    # Updates
    create_update,
    edit_update,
    get_updates_by_user,
    get_team_updates_by_date,
    get_missing_users_today,
    # Meeting notes
    upsert_meeting_notes,
    get_meeting_notes,
)
from email_utils import send_email

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Team Update Tracker API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)



class UserCreate(BaseModel):
    name: str
    email: str
    password: str
    role: str 
    team_id: int | None = None


class UserLogin(BaseModel):
    email: str
    password: str


class UpdateCreate(BaseModel):
    user_id: int
    content: str  
    date: str    


class UpdateEdit(BaseModel):
    content: str


class MeetingNoteUpsert(BaseModel):
    team_id: int
    date: str
    content: str
    created_by: int


class EmailSend(BaseModel):
    to_email: str
    subject: str
    body: str
    cc_emails: list[str] = []



@app.post("/auth/register")
def register(user: UserCreate):
    """Create a new user. Returns {id, name, email, role, team_id}."""
    password_hash = hash_password(user.password)
    try:
        user_id = create_user(
            name=user.name,
            email=user.email,
            password_hash=password_hash,
            role=user.role,
            team_id=user.team_id,
        )
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="Email already registered.")

    return {
        "id": user_id,
        "name": user.name,
        "email": user.email,
        "role": user.role,
        "team_id": user.team_id,
    }


@app.post("/auth/login")
def login(credentials: UserLogin):
    """Verify credentials. Returns user info or 401."""
    row = get_user_by_email(credentials.email)
    if row is None or not verify_password(credentials.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password.")

    user = dict(row)
    user.pop("password_hash", None)
    return user


@app.get("/teams")
def list_teams():
    """Return all teams as [{id, name}]."""
    rows = get_all_teams()
    return [dict(r) for r in rows]


@app.get("/teams/{team_id}")
def get_team(team_id: int):
    """Return a single team by id."""
    row = get_team_by_id(team_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Team not found.")
    return dict(row)


@app.get("/users/team/{team_id}")
def users_by_team(team_id: int):
    """Return all users in a team."""
    rows = get_users_by_team(team_id)
    result = []
    for r in rows:
        user = dict(r)
        user.pop("password_hash", None)
        result.append(user)
    return result


@app.get("/users/leaders")
def leaders():
    """Return all users with role='leader'."""
    rows = get_leaders()
    result = []
    for r in rows:
        user = dict(r)
        user.pop("password_hash", None)
        result.append(user)
    return result

@app.post("/updates")
def create_new_update(update: UpdateCreate):
    """Create a daily update. Raises 400 if a duplicate (user_id, date) exists."""
    try:
        update_id = create_update(
            user_id=update.user_id,
            content=update.content,
            date=update.date,
        )
    except sqlite3.IntegrityError:
        raise HTTPException(
            status_code=400,
            detail="An update already exists for this user on the given date.",
        )
    return {"id": update_id, "user_id": update.user_id, "date": update.date}


@app.put("/updates/{update_id}")
def edit_existing_update(update_id: int, body: UpdateEdit):
    """Edit the content of an existing update (no delete allowed)."""
    edit_update(update_id=update_id, content=body.content)
    return {"id": update_id, "updated": True}


@app.get("/updates/user/{user_id}")
def updates_for_user(user_id: int):
    """Return all updates for a user, newest first."""
    rows = get_updates_by_user(user_id)
    return [dict(r) for r in rows]


@app.get("/updates/team/{team_id}")
def updates_for_team(team_id: int, date: str = str(date.today())):
    """Return all updates for a team on a given date (defaults to today)."""
    rows = get_team_updates_by_date(team_id=team_id, date=date)
    return [dict(r) for r in rows]


@app.get("/updates/missing/{team_id}")
def missing_updates(team_id: int, date: str = str(date.today())):
    """Return users in the team who have no update for the given date."""
    rows = get_missing_users_today(team_id=team_id, date=date)
    return [dict(r) for r in rows]


@app.post("/meeting-notes")
def upsert_notes(note: MeetingNoteUpsert):
    """Upsert meeting notes for a (team_id, date) pair."""
    upsert_meeting_notes(
        team_id=note.team_id,
        date=note.date,
        content=note.content,
        created_by=note.created_by,
    )
    return {"team_id": note.team_id, "date": note.date, "upserted": True}


@app.get("/meeting-notes/{team_id}")
def get_notes(team_id: int, date: str = str(date.today())):
    """Get meeting notes for a team on a given date."""
    row = get_meeting_notes(team_id=team_id, date=date)
    if row is None:
        raise HTTPException(status_code=404, detail="No meeting notes found.")
    return dict(row)


@app.post("/email/send")
def send_email_endpoint(payload: EmailSend):
    """Send an email with optional CC. Returns {success, message}."""
    success, message = send_email(
        to_email=payload.to_email,
        subject=payload.subject,
        body=payload.body,
        cc_emails=payload.cc_emails,
    )
    return {"success": success, "message": message}