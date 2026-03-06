"""Zendesk API client with dual-identity routing (plan §10.5, Layer 4).

Read operations run as the *read identity* (ideally a restricted Zendesk
user, e.g. a light agent); write operations run as the *write identity*
(full agent). If both emails are the same, Layer 4 is a no-op and the
client behaves like a single-identity client.
"""
from typing import Dict, Any, List
import json
import urllib.request
import urllib.parse
import base64
import requests as _requests

from zenpy import Zenpy
from zenpy.lib.api_objects import Comment
from zenpy.lib.api_objects import Ticket as ZenpyTicket


def _basic_auth_header(email: str, token: str) -> str:
    credentials = f"{email}/token:{token}"
    encoded = base64.b64encode(credentials.encode()).decode("ascii")
    return f"Basic {encoded}"


class ZendeskClient:
    def __init__(self, subdomain: str, token: str, read_email: str, write_email: str):
        """
        Initialize with dual identities. Pass the same email twice for
        single-identity mode.
        """
        self.subdomain = subdomain
        self.base_url = f"https://{subdomain}.zendesk.com/api/v2"
        self.dual_identity = read_email != write_email

        self._read = Zenpy(subdomain=subdomain, email=read_email, token=token)
        self._write = (
            Zenpy(subdomain=subdomain, email=write_email, token=token)
            if self.dual_identity
            else self._read
        )
        # Auth headers for direct API calls.
        self._read_auth_header = _basic_auth_header(read_email, token)
        self._write_auth_header = _basic_auth_header(write_email, token)

    # ------------------------------------------------------------------
    # Direct API helpers
    # ------------------------------------------------------------------

    def _api_get(self, path_and_query: str, auth_header: str | None = None) -> Dict[str, Any]:
        url = f"{self.base_url}/{path_and_query.lstrip('/')}"
        req = urllib.request.Request(url)
        req.add_header('Authorization', auth_header or self._read_auth_header)
        req.add_header('Content-Type', 'application/json')
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read().decode())

    @staticmethod
    def _cursor_sort(sort_by: str, sort_order: str) -> str:
        field = sort_by if sort_by in ('created_at', 'updated_at', 'priority', 'status', 'id') else 'created_at'
        return f"-{field}" if sort_order == 'desc' else field

    # ------------------------------------------------------------------
    # READ operations — routed through the read identity
    # ------------------------------------------------------------------

    def get_ticket(self, ticket_id: int) -> Dict[str, Any]:
        """Query a ticket by its ID."""
        try:
            ticket = self._read.tickets(id=ticket_id)
            return {
                'id': ticket.id,
                'subject': ticket.subject,
                'description': ticket.description,
                'status': ticket.status,
                'priority': ticket.priority,
                'created_at': str(ticket.created_at),
                'updated_at': str(ticket.updated_at),
                'requester_id': ticket.requester_id,
                'assignee_id': ticket.assignee_id,
                'organization_id': ticket.organization_id
            }
        except Exception as e:
            raise Exception(f"Failed to get ticket {ticket_id}: {str(e)}")

    def get_ticket_comments(self, ticket_id: int, per_page: int = 50,
                            cursor: str | None = None) -> Dict[str, Any]:
        """Get comments for a ticket with cursor pagination, including attachment metadata."""
        try:
            per_page = min(max(per_page, 1), 100)
            params = {'page[size]': str(per_page)}
            if cursor:
                params['page[after]'] = cursor
            data = self._api_get(
                f"tickets/{ticket_id}/comments.json?{urllib.parse.urlencode(params)}"
            )
            result = []
            for comment in data.get('comments', []):
                attachments = [{
                    'id': a.get('id'),
                    'file_name': a.get('file_name'),
                    'content_url': a.get('content_url'),
                    'content_type': a.get('content_type'),
                    'size': a.get('size'),
                } for a in comment.get('attachments') or []]
                result.append({
                    'id': comment.get('id'),
                    'author_id': comment.get('author_id'),
                    'body': comment.get('body'),
                    'html_body': comment.get('html_body'),
                    'public': comment.get('public'),
                    'created_at': str(comment.get('created_at')),
                    'attachments': attachments,
                })
            meta = data.get('meta', {})
            return {
                'comments': result,
                'count': len(result),
                'has_more': meta.get('has_more', False),
                'after_cursor': meta.get('after_cursor'),
            }
        except Exception as e:
            raise Exception(f"Failed to get comments for ticket {ticket_id}: {str(e)}")

    def search_tickets(self, query: str, limit: int = 25) -> List[Dict[str, Any]]:
        """
        Search tickets using the Zendesk Search API query syntax,
        e.g. 'status:open priority:high', 'requester:user@example.com printer'.
        """
        try:
            limit = min(max(limit, 1), 100)
            results = self._read.search(type='ticket', query=query)
            tickets = []
            for ticket in results:
                tickets.append({
                    'id': ticket.id,
                    'subject': ticket.subject,
                    'status': ticket.status,
                    'priority': ticket.priority,
                    'created_at': str(ticket.created_at),
                    'updated_at': str(ticket.updated_at),
                    'requester_id': ticket.requester_id,
                    'assignee_id': ticket.assignee_id,
                })
                if len(tickets) >= limit:
                    break
            return tickets
        except Exception as e:
            raise Exception(f"Failed to search tickets: {str(e)}")

    # Allowed image MIME types. SVG is excluded — it can contain active XML/JS content.
    _ALLOWED_IMAGE_TYPES = {'image/jpeg', 'image/png', 'image/gif', 'image/webp'}

    # Magic bytes (file signatures) for each allowed type.
    _MAGIC_BYTES: Dict[str, List[bytes]] = {
        'image/jpeg': [b'\xff\xd8\xff'],
        'image/png':  [b'\x89PNG\r\n\x1a\n'],
        'image/gif':  [b'GIF87a', b'GIF89a'],
        'image/webp': [b'RIFF'],  # RIFF....WEBP — checked further below
    }

    # 10 MB hard cap to guard against image bombs and token budget blowout.
    _MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024

    def get_ticket_attachment(self, content_url: str) -> Dict[str, Any]:
        """
        Fetch an image attachment and return base64-encoded data.

        Security measures applied:
        - Allowlist of safe image MIME types (no SVG or arbitrary binary).
        - Magic byte validation so the file header must match the declared type.
        - 10 MB size cap to prevent image bombs and excessive token usage.
        """
        try:
            response = _requests.get(
                content_url,
                headers={'Authorization': self._read_auth_header},
                timeout=30,
                stream=True,
            )
            response.raise_for_status()

            content_type = response.headers.get('Content-Type', '').split(';')[0].strip().lower()

            if content_type not in self._ALLOWED_IMAGE_TYPES:
                raise ValueError(
                    f"Attachment type '{content_type}' is not allowed. "
                    f"Supported types: {sorted(self._ALLOWED_IMAGE_TYPES)}"
                )

            chunks = []
            total = 0
            for chunk in response.iter_content(chunk_size=65536):
                total += len(chunk)
                if total > self._MAX_ATTACHMENT_BYTES:
                    raise ValueError(
                        f"Attachment exceeds the {self._MAX_ATTACHMENT_BYTES // (1024*1024)} MB size limit."
                    )
                chunks.append(chunk)
            content = b''.join(chunks)

            magic_signatures = self._MAGIC_BYTES.get(content_type, [])
            if magic_signatures and not any(content.startswith(sig) for sig in magic_signatures):
                raise ValueError(
                    f"File header does not match declared content type '{content_type}'. "
                    "The attachment may be spoofed."
                )
            if content_type == 'image/webp' and content[8:12] != b'WEBP':
                raise ValueError("File header does not match declared content type 'image/webp'.")

            return {
                'data': base64.b64encode(content).decode('ascii'),
                'content_type': content_type,
            }
        except (ValueError, _requests.HTTPError):
            raise
        except Exception as e:
            raise Exception(f"Failed to fetch attachment from {content_url}: {str(e)}")

    def get_tickets(self, per_page: int = 25, cursor: str | None = None,
                    sort_by: str = 'created_at', sort_order: str = 'desc') -> Dict[str, Any]:
        """Get tickets using Zendesk cursor pagination (offset pagination is deprecated).

        Pass the returned `after_cursor` back as `cursor` to fetch the next page.
        """
        try:
            per_page = min(max(per_page, 1), 100)
            params = {
                'page[size]': str(per_page),
                'sort': self._cursor_sort(sort_by, sort_order),
            }
            if cursor:
                params['page[after]'] = cursor
            data = self._api_get(f"tickets.json?{urllib.parse.urlencode(params)}")

            ticket_list = [{
                'id': t.get('id'),
                'subject': t.get('subject'),
                'status': t.get('status'),
                'priority': t.get('priority'),
                'description': t.get('description'),
                'created_at': t.get('created_at'),
                'updated_at': t.get('updated_at'),
                'requester_id': t.get('requester_id'),
                'assignee_id': t.get('assignee_id'),
            } for t in data.get('tickets', [])]

            meta = data.get('meta', {})
            return {
                'tickets': ticket_list,
                'count': len(ticket_list),
                'per_page': per_page,
                'sort_by': sort_by,
                'sort_order': sort_order,
                'has_more': meta.get('has_more', False),
                'after_cursor': meta.get('after_cursor'),
            }
        except urllib.error.HTTPError as e:
            error_body = e.read().decode() if e.fp else "No response body"
            raise Exception(f"Failed to get latest tickets: HTTP {e.code} - {e.reason}. {error_body}")
        except Exception as e:
            raise Exception(f"Failed to get latest tickets: {str(e)}")

    def get_all_articles(self) -> Dict[str, Any]:
        """Fetch help center articles as knowledge base. Returns Dict of section -> [article]."""
        try:
            sections = self._read.help_center.sections()

            kb = {}
            for section in sections:
                articles = self._read.help_center.sections.articles(section.id)
                kb[section.name] = {
                    'section_id': section.id,
                    'description': section.description,
                    'articles': [{
                        'id': article.id,
                        'title': article.title,
                        'body': article.body,
                        'updated_at': str(article.updated_at),
                        'url': article.html_url
                    } for article in articles]
                }

            return kb
        except Exception as e:
            raise Exception(f"Failed to fetch knowledge base: {str(e)}")

    def get_user(self, user_id: int) -> Dict[str, Any]:
        """Get a Zendesk user by ID (resolves requester/assignee IDs to people)."""
        try:
            user = self._read.users(id=user_id)
            return {
                'id': user.id,
                'name': user.name,
                'email': user.email,
                'role': user.role,
                'organization_id': user.organization_id,
                'created_at': str(user.created_at),
                'suspended': user.suspended,
            }
        except Exception as e:
            raise Exception(f"Failed to get user {user_id}: {str(e)}")

    def search_users(self, query: str, limit: int = 25) -> List[Dict[str, Any]]:
        """Search users by name or email."""
        try:
            limit = min(max(limit, 1), 100)
            results = self._read.search(type='user', query=query)
            users = []
            for user in results:
                users.append({
                    'id': user.id,
                    'name': user.name,
                    'email': user.email,
                    'role': user.role,
                    'organization_id': user.organization_id,
                })
                if len(users) >= limit:
                    break
            return users
        except Exception as e:
            raise Exception(f"Failed to search users: {str(e)}")

    def list_groups(self) -> List[Dict[str, Any]]:
        """List all agent groups (for routing tickets to teams)."""
        try:
            return [{
                'id': g.id,
                'name': g.name,
                'description': getattr(g, 'description', None),
                'deleted': getattr(g, 'deleted', False),
            } for g in self._read.groups()]
        except Exception as e:
            raise Exception(f"Failed to list groups: {str(e)}")

    def list_ticket_fields(self) -> List[Dict[str, Any]]:
        """List ticket fields incl. custom fields — needed to interpret custom_fields {id, value} pairs."""
        try:
            fields = []
            for f in self._read.ticket_fields():
                field = {
                    'id': f.id,
                    'type': f.type,
                    'title': f.title,
                    'description': getattr(f, 'description', None),
                    'active': getattr(f, 'active', True),
                    'required': getattr(f, 'required', False),
                }
                options = getattr(f, 'custom_field_options', None)
                if options:
                    field['options'] = [
                        {'name': o.name, 'value': o.value} for o in options
                    ]
                fields.append(field)
            return fields
        except Exception as e:
            raise Exception(f"Failed to list ticket fields: {str(e)}")

    def search_articles(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Search Help Center articles (context-safe alternative to dumping the whole KB)."""
        try:
            limit = min(max(limit, 1), 25)
            params = urllib.parse.urlencode({'query': query, 'per_page': str(limit)})
            data = self._api_get(f"help_center/articles/search.json?{params}")
            return [{
                'id': a.get('id'),
                'title': a.get('title'),
                'body': a.get('body'),
                'section_id': a.get('section_id'),
                'updated_at': a.get('updated_at'),
                'url': a.get('html_url'),
            } for a in data.get('results', [])]
        except Exception as e:
            raise Exception(f"Failed to search articles: {str(e)}")

    # ------------------------------------------------------------------
    # WRITE operations — routed through the write identity
    # ------------------------------------------------------------------

    _MAX_UPLOAD_BYTES = 10 * 1024 * 1024

    def upload_attachment(self, file_name: str, data_base64: str,
                          content_type: str = "application/octet-stream") -> Dict[str, Any]:
        """Upload a file to Zendesk; returns an upload token to attach to a comment."""
        try:
            content = base64.b64decode(data_base64)
            if len(content) > self._MAX_UPLOAD_BYTES:
                raise ValueError(
                    f"Upload exceeds the {self._MAX_UPLOAD_BYTES // (1024*1024)} MB size limit."
                )
            params = urllib.parse.urlencode({'filename': file_name})
            response = _requests.post(
                f"{self.base_url}/uploads.json?{params}",
                headers={
                    'Authorization': self._write_auth_header,
                    'Content-Type': content_type,
                },
                data=content,
                timeout=60,
            )
            response.raise_for_status()
            upload = response.json().get('upload', {})
            return {
                'token': upload.get('token'),
                'file_name': file_name,
                'size': len(content),
            }
        except ValueError:
            raise
        except Exception as e:
            raise Exception(f"Failed to upload attachment '{file_name}': {str(e)}")

    def post_comment(self, ticket_id: int, comment: str, public: bool = True,
                     upload_tokens: List[str] | None = None) -> str:
        """Post a comment to an existing ticket, optionally attaching uploaded files."""
        try:
            ticket = self._write.tickets(id=ticket_id)
            ticket.comment = Comment(
                html_body=comment,
                public=public,
                uploads=upload_tokens or [],
            )
            self._write.tickets.update(ticket)
            return comment
        except Exception as e:
            raise Exception(f"Failed to post comment on ticket {ticket_id}: {str(e)}")

    def create_ticket(
        self,
        subject: str,
        description: str,
        requester_id: int | None = None,
        assignee_id: int | None = None,
        priority: str | None = None,
        type: str | None = None,
        tags: List[str] | None = None,
        custom_fields: List[Dict[str, Any]] | None = None,
    ) -> Dict[str, Any]:
        """Create a new Zendesk ticket and return essential fields."""
        try:
            ticket = ZenpyTicket(
                subject=subject,
                description=description,
                requester_id=requester_id,
                assignee_id=assignee_id,
                priority=priority,
                type=type,
                tags=tags,
                custom_fields=custom_fields,
            )
            created_audit = self._write.tickets.create(ticket)
            created_ticket_id = getattr(getattr(created_audit, 'ticket', None), 'id', None)
            if created_ticket_id is None:
                created_ticket_id = getattr(created_audit, 'id', None)

            created = self._write.tickets(id=created_ticket_id) if created_ticket_id else None

            return {
                'id': getattr(created, 'id', created_ticket_id),
                'subject': getattr(created, 'subject', subject),
                'description': getattr(created, 'description', description),
                'status': getattr(created, 'status', 'new'),
                'priority': getattr(created, 'priority', priority),
                'type': getattr(created, 'type', type),
                'created_at': str(getattr(created, 'created_at', '')),
                'updated_at': str(getattr(created, 'updated_at', '')),
                'requester_id': getattr(created, 'requester_id', requester_id),
                'assignee_id': getattr(created, 'assignee_id', assignee_id),
                'organization_id': getattr(created, 'organization_id', None),
                'tags': list(getattr(created, 'tags', tags or []) or []),
            }
        except Exception as e:
            raise Exception(f"Failed to create ticket: {str(e)}")

    def update_ticket(self, ticket_id: int, **fields: Any) -> Dict[str, Any]:
        """Update a Zendesk ticket with the provided fields."""
        try:
            ticket = self._write.tickets(id=ticket_id)
            for key, value in fields.items():
                if value is None:
                    continue
                setattr(ticket, key, value)

            # Returns a TicketAudit (not a Ticket) — don't read attrs from it.
            self._write.tickets.update(ticket)

            refreshed = self._write.tickets(id=ticket_id)

            return {
                'id': refreshed.id,
                'subject': refreshed.subject,
                'description': refreshed.description,
                'status': refreshed.status,
                'priority': refreshed.priority,
                'type': getattr(refreshed, 'type', None),
                'created_at': str(refreshed.created_at),
                'updated_at': str(refreshed.updated_at),
                'requester_id': refreshed.requester_id,
                'assignee_id': refreshed.assignee_id,
                'organization_id': refreshed.organization_id,
                'tags': list(getattr(refreshed, 'tags', []) or []),
            }
        except Exception as e:
            raise Exception(f"Failed to update ticket {ticket_id}: {str(e)}")
