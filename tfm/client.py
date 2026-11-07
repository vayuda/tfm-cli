"""Thin HTTP client for the Terraforming Mars web app's JSON API."""
import json
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_SERVER = "https://terraforming-mars.herokuapp.com"
USER_AGENT = "tfm-cli/0.1"


class TfmError(Exception):
    pass


def parse_link(link, server=None):
    """Split a player link (or bare player id) into (server, player_id)."""
    link = link.strip()
    if "://" not in link:
        if not link.startswith("p"):
            raise TfmError(f"not a player link or id: {link}")
        return (server or DEFAULT_SERVER).rstrip("/"), link
    url = urllib.parse.urlparse(link)
    ids = urllib.parse.parse_qs(url.query).get("id")
    if not ids:
        raise TfmError(f"link has no ?id=: {link}")
    if ids[0].startswith("s"):
        raise TfmError("that is a spectator link; use the player link (id starts with 'p')")
    return f"{url.scheme}://{url.netloc}", ids[0]


class Client:
    def __init__(self, server, player_id, timeout=30):
        self.server = server.rstrip("/")
        self.player_id = player_id
        self.timeout = timeout

    def _request(self, path, params=None, body=None):
        query = urllib.parse.urlencode({"id": self.player_id, **(params or {})})
        url = f"{self.server}/{path}?{query}"
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method="POST" if body is not None else "GET")
        req.add_header("User-Agent", USER_AGENT)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        # A dropped connection (seen behind proxies) is retried once for reads. A submit is not retried: the
        # server may already have applied it.
        for attempt in (1, 2):
            try:
                raw = self._open(req)
                break
            except (ConnectionError, TimeoutError) as e:
                if body is not None:
                    raise TfmError(f"connection to {self.server} dropped ({e.__class__.__name__}) while submitting; "
                                   "the move may have been applied: run `tfm turn` to check before retrying") from None
                if attempt == 2:
                    raise TfmError(f"connection to {self.server} dropped twice ({e.__class__.__name__}); "
                                   "run the command again") from None
        return json.loads(raw) if raw else None

    def _open(self, req):
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read().decode()
        except urllib.error.HTTPError as e:
            raw = e.read().decode(errors="replace")
            try:
                msg = json.loads(raw).get("message", raw)
            except (ValueError, AttributeError):
                msg = raw
            raise TfmError(f"server rejected request ({e.code}): {msg}") from None
        except urllib.error.URLError as e:
            if isinstance(e.reason, (ConnectionError, TimeoutError)):
                raise e.reason from None
            raise TfmError(f"cannot reach {self.server}: {e.reason}") from None

    def player(self):
        return self._request("api/player")

    def waiting_for(self, game_age, undo_count):
        return self._request("api/waitingfor", {"gameAge": game_age, "undoCount": undo_count})

    def logs(self, generation=None):
        return self._request("api/game/logs", {} if generation is None else {"generation": generation})

    def submit(self, response, run_id=None):
        body = dict(response)
        if run_id is not None:
            body["runId"] = run_id
        return self._request("player/input", body=body)


def create_game(server, settings):
    req = urllib.request.Request(
        f"{server.rstrip('/')}/api/creategame",
        data=json.dumps(settings).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise TfmError(f"create game failed ({e.code}): {e.read().decode(errors='replace')}") from None
    except urllib.error.URLError as e:
        raise TfmError(f"cannot reach {server}: {e.reason}") from None
