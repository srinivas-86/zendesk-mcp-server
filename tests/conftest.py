import pytest

from zendesk_mcp_server.config import Settings


class FakeZendeskClient:
    """In-memory stand-in for ZendeskClient — no network."""

    dual_identity = True

    def __init__(self):
        self.calls = []

    def get_ticket(self, ticket_id):
        self.calls.append(("get_ticket", ticket_id))
        return {"id": ticket_id, "subject": "Test ticket", "status": "open"}

    def get_tickets(self, per_page=25, cursor=None, sort_by="created_at", sort_order="desc"):
        self.calls.append(("get_tickets", cursor))
        return {"tickets": [{"id": 1}], "count": 1, "has_more": False, "after_cursor": "abc"}

    def get_ticket_comments(self, ticket_id, per_page=50, cursor=None):
        self.calls.append(("get_ticket_comments", ticket_id))
        return {
            "comments": [{"id": 10, "body": "hello", "attachments": []}],
            "count": 1,
            "has_more": False,
            "after_cursor": None,
        }

    def get_user(self, user_id):
        self.calls.append(("get_user", user_id))
        return {"id": user_id, "name": "Jane", "email": "jane@example.com", "role": "agent"}

    def search_users(self, query, limit=25):
        self.calls.append(("search_users", query))
        return [{"id": 3, "name": "Jane", "email": "jane@example.com"}]

    def list_groups(self):
        self.calls.append(("list_groups",))
        return [{"id": 1, "name": "Support"}]

    def list_ticket_fields(self):
        self.calls.append(("list_ticket_fields",))
        return [{"id": 100, "type": "tagger", "title": "Product"}]

    def search_articles(self, query, limit=10):
        self.calls.append(("search_articles", query))
        return [{"id": 7, "title": "How to reset password"}]

    def upload_attachment(self, file_name, data_base64, content_type="application/octet-stream"):
        self.calls.append(("upload_attachment", file_name))
        return {"token": "tok123", "file_name": file_name, "size": 2}

    def search_tickets(self, query, limit=25):
        self.calls.append(("search_tickets", query))
        return [{"id": 2, "subject": "match"}]

    def get_ticket_attachment(self, content_url):
        self.calls.append(("get_ticket_attachment", content_url))
        return {"data": "aGk=", "content_type": "image/png"}

    def create_ticket(self, **kwargs):
        self.calls.append(("create_ticket", kwargs.get("subject")))
        return {"id": 99, "subject": kwargs.get("subject"), "status": "new"}

    def update_ticket(self, ticket_id, **fields):
        self.calls.append(("update_ticket", ticket_id))
        return {"id": ticket_id, "status": fields.get("status") or "open"}

    def post_comment(self, ticket_id, comment, public=True, upload_tokens=None):
        self.calls.append(("post_comment", ticket_id, public))
        return comment

    def get_all_articles(self):
        self.calls.append(("get_all_articles",))
        return {"General": {"section_id": 1, "description": "", "articles": []}}


@pytest.fixture
def fake_client():
    return FakeZendeskClient()


@pytest.fixture
def settings(tmp_path):
    return Settings(
        subdomain="example",
        api_token="secret",
        read_email="reader@example.com",
        write_email="writer@example.com",
        transport="stdio",
        auth_enabled=False,
        keys_db=str(tmp_path / "keys.db"),
    )
