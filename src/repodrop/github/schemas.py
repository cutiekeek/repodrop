"""Pydantic models for the subset of GitHub REST responses we use."""

from datetime import datetime

from pydantic import BaseModel, TypeAdapter


class User(BaseModel):
    login: str
    html_url: str
    avatar_url: str


class Repository(BaseModel):
    id: int
    full_name: str
    private: bool
    default_branch: str
    html_url: str
    description: str | None = None
    owner: User


class Release(BaseModel):
    id: int
    tag_name: str
    name: str | None = None
    body: str | None = None
    draft: bool
    prerelease: bool
    html_url: str
    created_at: datetime
    published_at: datetime | None = None
    author: User | None = None


class TagCommit(BaseModel):
    sha: str


class Tag(BaseModel):
    name: str
    commit: TagCommit


class GitActor(BaseModel):
    name: str | None = None
    date: datetime | None = None


class CommitDetail(BaseModel):
    message: str
    author: GitActor | None = None


class Commit(BaseModel):
    sha: str
    html_url: str
    commit: CommitDetail
    author: User | None = None  # null when the commit email isn't linked to a GitHub account


releases_adapter = TypeAdapter(list[Release])
tags_adapter = TypeAdapter(list[Tag])
commits_adapter = TypeAdapter(list[Commit])
