"""
Base scraper interface.
"""
from abc import ABC, abstractmethod
from typing import Optional, Tuple
from ..core.models import PostData, AccountData
from ..core.utils import make_session
import requests


class BaseScraper(ABC):
    platform: str = "unknown"

    def __init__(self, session: Optional[requests.Session] = None, config: dict = None):
        self.session = session or make_session()
        self.config = config or {}

    @abstractmethod
    def get_post(self, post_id: str, username: Optional[str] = None) -> Optional[PostData]:
        pass

    @abstractmethod
    def get_account(self, username: str) -> Optional[AccountData]:
        pass

    def get_post_and_account(
        self, post_id: str, username: Optional[str] = None
    ) -> Tuple[Optional[PostData], Optional[AccountData]]:
        post = self.get_post(post_id, username)
        account = None
        if post and post.author_username:
            account = self.get_account(post.author_username)
        elif username:
            account = self.get_account(username)
        return post, account
