import html
import json
import logging
import os
import re
import signal
import sqlite3
import time
import urllib.error
import urllib.request
import secrets
from pathlib import Path

COMMANDS = [
    {'command': command, 'description': description}
    for command, description in [
        ('start', 'Welcome and instructions'),
        ('commands', 'Show all commands and examples'),
        ('help', 'Show help and scoring rules'),
        ('join', 'Join this group’s league'),
        ('result', 'Choose players and enter the score'),
        ('head2head', 'Compare two players head to head'),
        ('cancel', 'Cancel your current player selection'),
        ('table', 'Show the league standings'),
        ('matches', 'Show recent games and match IDs'),
        ('delete', 'Delete a match: /delete 12'),
        ('newseason', 'Start a fresh season and keep history'),
        ('chatid', 'Show this group’s Telegram ID'),
    ]
]

HELP = """<h2>⚽ FC league</h2>
<p>Three friends. Every goal counts.</p>
<h3>Record a game</h3>
<p>Each player sends /join once. Then send /result, choose both players using the buttons, and reply with <code>3 1</code> (first player’s score first).</p>
<p>/cancel stops your current entry. No need to tag yourself.</p>
<p>The shortcut <code>@ali 3 @sam 1</code> still works.</p>
<p>Names work too: <code>/result Ali 3 Sam 1</code>. Use the same names each time.</p>
<h3>Your league</h3>
<ul>
<li>/table — standings</li>
<li>/matches — last 10 games and match IDs</li>
<li>/head2head — choose two players and compare their season</li>
<li><code>/delete 12</code> — remove an incorrect result</li>
<li>/newseason — fresh standings, history saved</li>
</ul>
<details><summary>Scoring and history</summary>
<p><b>Win · 3 pts</b> &nbsp; Draw · 1 pt &nbsp; Loss · 0 pts</p>
<p>Ranked by points → goal difference → goals scored. Exact ties display alphabetically.</p>
<p>View an old season with <code>/table 1</code> or <code>/matches 1</code>.</p>
</details>
<details><summary>More commands</summary>
<p>/start — welcome<br>/commands — command guide<br>/help — help<br>/chatid — group ID</p>
</details>
<footer>Everyone in the group can record games and make corrections.</footer>"""


def connect(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS groups(chat INTEGER PRIMARY KEY, season INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS matches(
          id INTEGER PRIMARY KEY, chat INTEGER NOT NULL, season INTEGER NOT NULL,
          home TEXT NOT NULL, away TEXT NOT NULL, hg INTEGER NOT NULL, ag INTEGER NOT NULL,
          created TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE INDEX IF NOT EXISTS match_group ON matches(chat, season);
        CREATE TABLE IF NOT EXISTS updates(id INTEGER PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS players(chat INTEGER, user INTEGER, name TEXT NOT NULL, username TEXT,
          PRIMARY KEY(chat,user));
        CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, chat INTEGER, owner INTEGER,
          season INTEGER, thread INTEGER, home TEXT, away TEXT, prompt INTEGER,
          created INTEGER NOT NULL);
    """)
    if 'mode' not in {row[1] for row in db.execute('PRAGMA table_info(sessions)')}:
        db.execute("ALTER TABLE sessions ADD COLUMN mode TEXT NOT NULL DEFAULT 'result'")
        db.commit()
    return db


def player(value):
    value = value.lstrip('@').casefold()
    if not re.fullmatch(r'[\w.-]{1,32}', value):
        raise ValueError('Use a username or a single name (letters, numbers, dots, underscores).')
    return value


def display(db, chat, key):
    row = db.execute('SELECT name FROM players WHERE chat=? AND ? = \'tg:\' || user', (chat,key)).fetchone()
    return row[0] if row else key


def player_key(db, chat, value):
    value = player(value)
    rows = db.execute('SELECT user FROM players WHERE chat=? AND username=?', (chat,value)).fetchall()
    return f"tg:{rows[0][0]}" if len(rows) == 1 else value


def save_result(db, chat, season, home, hg, away, ag):
    if home == away:
        raise ValueError('Choose two different players.')
    if not (0 <= hg <= 999 and 0 <= ag <= 999):
        raise ValueError('Scores must be between 0 and 999.')
    mid = db.execute('INSERT INTO matches(chat,season,home,away,hg,ag) VALUES(?,?,?,?,?,?)', (chat,season,home,away,hg,ag)).lastrowid
    h, a = html.escape(display(db,chat,home)), html.escape(display(db,chat,away))
    outcome = 'A point each. 🤝' if hg == ag else f'{h if hg > ag else a} takes 3 points.'
    return f'<h3>✅ Saved · Match #{mid}</h3><p><b>{h} &nbsp; {hg} – {ag} &nbsp; {a}</b></p><p>{outcome}</p><hr>' + table(db,chat,season)


def picker(db, session):
    players = db.execute('SELECT * FROM players WHERE chat=? ORDER BY name,user', (session['chat'],)).fetchall()
    stage = 'pick2' if session['home'] else 'pick1'
    buttons = [[{'text': p['name'], 'callback_data': f"{stage}:{session['id']}:{p['user']}"}] for p in players if f"tg:{p['user']}" != session['home']]
    buttons.append([{'text': 'Cancel', 'callback_data': f"cancel:{session['id']}"}])
    title = 'Choose the first player' if not session['home'] else f"{display(db, session['chat'], session['home'])} vs… Choose the second player"
    if session['mode'] == 'head2head':
        title = '⚔️ Head to head · ' + title
    return {'text': title, 'reply_markup': {'inline_keyboard': buttons}}


def head2head(db, chat, season, home, away):
    games = db.execute('''SELECT * FROM matches WHERE chat=? AND season=?
        AND ((home=? AND away=?) OR (home=? AND away=?)) ORDER BY id DESC''',
        (chat,season,home,away,away,home)).fetchall()
    h, a = html.escape(display(db,chat,home)), html.escape(display(db,chat,away))
    title = f'<h2>⚔️ Head to head</h2><p><b>{h} vs {a}</b> · Season {season}</p>'
    if not games:
        return title + '<p>No games between these players this season. Send /result to record their first meeting.</p>'
    wins = losses = draws = gf = ga = 0
    recent = []
    for m in games:
        hg, ag = (m['hg'],m['ag']) if m['home'] == home else (m['ag'],m['hg'])
        wins += hg > ag
        losses += hg < ag
        draws += hg == ag
        gf += hg
        ga += ag
        if len(recent) < 10:
            recent.append(f"<tr><td>#{m['id']}</td><td>{hg}</td><td>{ag}</td></tr>")
    summary = ('<table bordered striped compact>'
               f'<tr><th>Stat</th><th>{h}</th><th>{a}</th></tr>'
               f'<tr><td>Wins</td><td><b>{wins}</b></td><td><b>{losses}</b></td></tr>'
               f'<tr><td>Draws</td><td>{draws}</td><td>{draws}</td></tr>'
               f'<tr><td>Goals</td><td>{gf}</td><td>{ga}</td></tr>'
               f'<tr><td>Points</td><td>{3*wins+draws}</td><td>{3*losses+draws}</td></tr></table>')
    verdict = 'All square on wins. 🤝' if wins == losses else f'{h if wins > losses else a} leads on wins.'
    return (title + f'<p>{len(games)} games · {verdict}</p>' + summary
            + f'<h3>Recent meetings</h3><table striped compact><tr><th>Match</th><th>{h}</th><th>{a}</th></tr>'
            + ''.join(recent) + '</table>')


def callback(db, query):
    message = query.get('message', {})
    chat = message.get('chat', {}).get('id')
    parts = query.get('data','').split(':')
    if len(parts) not in (2,3) or parts[0] not in ('pick1','pick2','cancel'):
        return None, 'Unknown button.'
    session = db.execute('SELECT * FROM sessions WHERE id=? AND chat=?', (parts[1],chat)).fetchone()
    if not session or time.time() - session['created'] > 1800:
        return None, 'This entry expired. Send /result or /head2head again.'
    if session['owner'] != query['from']['id']:
        return None, 'This is someone else’s entry. Send /result or /head2head to start yours.'
    current = db.execute('SELECT season FROM groups WHERE chat=?', (chat,)).fetchone()[0]
    if current != session['season']:
        db.execute('DELETE FROM sessions WHERE id=?', (session['id'],))
        return None, 'The season changed. Start your selection again.'
    if parts[0] == 'cancel':
        db.execute('DELETE FROM sessions WHERE id=?', (session['id'],))
        return '<p>Selection cancelled.</p>', 'Cancelled.'
    if (parts[0] == 'pick1' and session['home']) or (parts[0] == 'pick2' and not session['home']):
        return None, 'That selection was already handled. Use the latest buttons.'
    if len(parts) != 3 or not parts[2].isdigit():
        return None, 'Invalid player.'
    selected = db.execute('SELECT user FROM players WHERE chat=? AND user=?', (chat,int(parts[2]))).fetchone()
    if not selected:
        return None, 'That player is not registered here.'
    key = f'tg:{selected[0]}'
    if session['away']:
        return None, 'Players already chosen. Reply to the score prompt.'
    if key == session['home']:
        return None, 'Choose a different opponent.'
    if not session['home']:
        db.execute('UPDATE sessions SET home=? WHERE id=?', (key,session['id']))
        return picker(db,db.execute('SELECT * FROM sessions WHERE id=?', (session['id'],)).fetchone()), ''
    if session['mode'] == 'head2head':
        reply = head2head(db,chat,session['season'],session['home'],key)
        db.execute('DELETE FROM sessions WHERE id=?', (session['id'],))
        return reply, ''
    db.execute('UPDATE sessions SET away=? WHERE id=?', (key,session['id']))
    h, a = display(db,chat,session['home']), display(db,chat,key)
    return {'text': f'{h} vs {a}\nReply with both scores in that order, e.g. 3 1 (or 3-1).\n/cancel to stop.',
            'reply_markup': {'force_reply': True, 'selective': False, 'input_field_placeholder': '3 1'}, '_session': session['id']}, ''


def table(db, chat, season):
    rows = {}
    for m in db.execute('SELECT * FROM matches WHERE chat=? AND season=?', (chat, season)):
        for name, gf, ga in [(m['home'], m['hg'], m['ag']), (m['away'], m['ag'], m['hg'])]:
            r = rows.setdefault(name, dict(P=0, W=0, D=0, L=0, GF=0, GA=0, GD=0, Pts=0))
            r['P'] += 1
            r['W'] += gf > ga
            r['D'] += gf == ga
            r['L'] += gf < ga
            r['GF'] += gf
            r['GA'] += ga
            r['GD'] += gf - ga
            r['Pts'] += 3 if gf > ga else int(gf == ga)
    if not rows:
        return f'<h2>🏆 Season {season}</h2><p>There are no games yet. Kick things off:</p><pre>/result @ali 3 @sam 1</pre>'
    ordered = sorted(rows.items(), key=lambda p: (-p[1]['Pts'], -p[1]['GD'], -p[1]['GF'], p[0]))
    columns = ('P', 'W', 'D', 'L', 'GF', 'GA', 'GD', 'Pts')
    lines = ['<table bordered striped compact><tr><th>#</th><th>Player</th>' + ''.join(f'<th>{key}</th>' for key in columns) + '</tr>']
    for rank, (name, r) in enumerate(ordered, 1):
        cells = ''.join(f'<td align="right">{r[key]}</td>' if key != 'Pts' else f'<td align="right"><b>{r[key]}</b></td>' for key in columns)
        lines.append(f'<tr><td>{rank}</td><td><b>{html.escape(display(db,chat,name))}</b></td>{cells}</tr>')
    lines.append('</table>')
    games = sum(r['P'] for r in rows.values()) // 2
    goals = sum(r['GF'] for r in rows.values())
    return (f'<h2>🏆 Season {season}</h2><p>{games} games · {goals} goals · {len(rows)} players</p>'
            + ''.join(lines))


def result_command(text):
    """Accept only a whole message with two @usernames and two integer scores."""
    match = re.fullmatch(r'\s*(@[A-Za-z0-9_]{1,32})\s+(-?[0-9]+)\s+(@[A-Za-z0-9_]{1,32})\s+(-?[0-9]+)\s*', text)
    return '/result ' + ' '.join(match.groups()) if match else None


def handle(db, chat, text, username, user=None, message=None):
    text = text.strip()
    if user and message and not text.startswith('/'):
        session = db.execute('SELECT * FROM sessions WHERE chat=? AND owner=? AND prompt=?',
                             (chat,user['id'],message.get('reply_to_message',{}).get('message_id',-1))).fetchone()
        if session:
            current = db.execute('SELECT season FROM groups WHERE chat=?', (chat,)).fetchone()[0]
            if time.time() - session['created'] > 1800 or current != session['season']:
                db.execute('DELETE FROM sessions WHERE id=?', (session['id'],))
                return '<p>This entry expired or the season changed. Send /result again.</p>'
            score = re.fullmatch(r'([0-9]{1,3})\s*(?:[-–:]\s*|\s+)([0-9]{1,3})', text)
            if not score:
                return {'text': 'Reply to the original score prompt with two scores, e.g. 3 1 or 3-1. Use /cancel to stop.'}
            reply = save_result(db,chat,session['season'],session['home'],int(score[1]),session['away'],int(score[2]))
            db.execute('DELETE FROM sessions WHERE id=?', (session['id'],))
            return reply
    if not text.startswith('/'):
        text = result_command(text)
        if text is None:
            return None
    parts = text.split()
    command = parts[0].split('@', 1)
    if len(command) == 2 and command[1].casefold() != username.casefold():
        return None
    cmd, args = command[0].lower(), parts[1:]
    db.execute('INSERT OR IGNORE INTO groups VALUES (?,1)', (chat,))
    season = db.execute('SELECT season FROM groups WHERE chat=?', (chat,)).fetchone()[0]
    if cmd in ('/help', '/start', '/commands'):
        return HELP
    if cmd == '/join':
        if not user or user.get('is_bot'):
            raise ValueError('Send /join from your personal Telegram account in the group.')
        name = ' '.join(filter(None, [user.get('first_name'),user.get('last_name')])) or user.get('username') or str(user['id'])
        uname = user.get('username','').casefold() or None
        # Preserve the existing username-based results when a player first joins.
        previous = db.execute('SELECT 1 FROM players WHERE chat=? AND user=?', (chat,user['id'])).fetchone()
        if not previous and uname:
            for column in ('home','away'):
                db.execute(f'UPDATE matches SET {column}=? WHERE chat=? AND {column}=?', (f"tg:{user['id']}",chat,uname))
        db.execute('INSERT INTO players(chat,user,name,username) VALUES(?,?,?,?) ON CONFLICT(chat,user) DO UPDATE SET name=excluded.name,username=excluded.username', (chat,user['id'],name,uname))
        return f'<h3>✅ {html.escape(name)} joined the league</h3><p>Send /result to pick the players and record a game.</p>'
    if cmd == '/cancel':
        if user:
            db.execute('DELETE FROM sessions WHERE chat=? AND owner=?', (chat,user['id']))
        return '<p>Selection cancelled.</p>'
    if cmd == '/chatid':
        return f'<h3>💬 Group ID</h3><p><code>{chat}</code></p>'
    if cmd in ('/result','/head2head'):
        if cmd == '/head2head' and args:
            raise ValueError('Use /head2head without arguments, then choose both players.')
        if not args:
            if not user:
                raise ValueError(f'Send {cmd} from your personal Telegram account.')
            players = db.execute('SELECT COUNT(*) FROM players WHERE chat=?', (chat,)).fetchone()[0]
            if players < 2:
                return '<p>At least two players need to send /join in this group first.</p>'
            db.execute('DELETE FROM sessions WHERE (chat=? AND owner=?) OR created<?', (chat,user['id'],int(time.time())-1800))
            sid = secrets.token_hex(8)
            db.execute('INSERT INTO sessions(id,chat,owner,season,thread,created,mode) VALUES(?,?,?,?,?,?,?)',
                       (sid,chat,user['id'],season,(message or {}).get('message_thread_id'),int(time.time()),cmd[1:]))
            return picker(db,db.execute('SELECT * FROM sessions WHERE id=?', (sid,)).fetchone())
        if len(args) != 4:
            raise ValueError('Use /result @ali 3 @sam 1')
        home, away = player_key(db,chat,args[0]), player_key(db,chat,args[2])
        if home == away:
            raise ValueError('Choose two different players.')
        try:
            hg, ag = int(args[1]), int(args[3])
        except ValueError:
            raise ValueError('Scores must be whole numbers.') from None
        if not (0 <= hg <= 999 and 0 <= ag <= 999):
            raise ValueError('Scores must be between 0 and 999.')
        return save_result(db,chat,season,home,hg,away,ag)
    if cmd in ('/table', '/matches'):
        if len(args) > 1 or (args and (not args[0].isdigit() or not 1 <= int(args[0]) <= season)):
            raise ValueError(f'Use {cmd} or {cmd} followed by an existing season number.')
        target = int(args[0]) if args else season
        if cmd == '/table':
            return table(db,chat,target)
        games = db.execute('SELECT * FROM matches WHERE chat=? AND season=? ORDER BY id DESC LIMIT 10', (chat,target)).fetchall()
        if not games:
            return f'<h2>🗓 Season {target} · Recent games</h2><p>No games yet. Use /result to add one.</p>'
        rows = ''.join(f"<tr><td>#{m['id']}</td><td>{html.escape(display(db,chat,m['home']))}</td><td><b>{m['hg']} – {m['ag']}</b></td><td>{html.escape(display(db,chat,m['away']))}</td></tr>" for m in games)
        return (f'<h2>🗓 Season {target} · Recent games</h2><table striped compact><tr><th>ID</th><th>Player</th><th>Score</th><th>Player</th></tr>'
                + rows + '</table><footer>Wrong score? Delete its ID with /delete, then record the corrected result.</footer>')
    if cmd == '/delete':
        if len(args) != 1 or not args[0].isdigit():
            raise ValueError('Use /delete 12 (find IDs with /matches).')
        result = db.execute('DELETE FROM matches WHERE id=? AND chat=?', (args[0],chat))
        return '<h3>🗑 Match deleted</h3>' + table(db,chat,season) if result.rowcount else '<p>Match not found in this group. Check /matches for the ID.</p>'
    if cmd == '/newseason':
        if args:
            raise ValueError('Use /newseason without arguments.')
        db.execute('UPDATE groups SET season=season+1 WHERE chat=?', (chat,))
        return f'<h2>🎉 Season {season+1} starts now</h2><p>A clean table. A new title to chase.</p><p>Record the opener with /result.</p><footer>History saved: /table {season} · /matches {season}</footer>'
    return '<p>Unknown command. Use /commands to see the guide.</p>'


def send_reply(api, chat, reply, thread=None):
    method = 'sendMessage' if isinstance(reply,dict) else 'sendRichMessage'
    data = {'chat_id': chat, **{k:v for k,v in reply.items() if not k.startswith('_')}} if isinstance(reply,dict) else {'chat_id': chat, 'rich_message': {'html': reply}}
    data['disable_notification'] = True
    if thread is not None:
        data['message_thread_id'] = thread
    return api(method, **data)


class APIError(Exception):
    pass


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    token = os.environ.get('BOT_TOKEN', '').strip()
    if not token:
        raise SystemExit('Set BOT_TOKEN in .env')
    path = Path(os.environ.get('DB_PATH','data/league.sqlite3'))
    path.parent.mkdir(parents=True, exist_ok=True)
    db = connect(path)
    allowed = os.environ.get('ALLOWED_CHAT_ID','').strip()
    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    def api(method, **data):
        request = urllib.request.Request(f'https://api.telegram.org/bot{token}/{method}', data=json.dumps(data).encode(), headers={'Content-Type':'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=40) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as error:
            # Never log exception URLs: they contain the bot token.
            raise APIError(f'Telegram HTTP {error.code}') from None
        except (OSError, ValueError):
            raise APIError('Telegram connection failed') from None
        if not payload.get('ok'):
            raise APIError('Telegram rejected the request')
        return payload['result']

    username = None
    while running:
        try:
            if username is None:
                bot_username = api('getMe')['username']
                api('setMyCommands', commands=COMMANDS)
                username = bot_username
                logging.info('Bot @%s ready', username)
            offset = db.execute('SELECT COALESCE(MAX(id),-1)+1 FROM updates').fetchone()[0]
            for update in api('getUpdates', offset=offset, timeout=25, allowed_updates=['message','callback_query']):
                if not running:
                    break
                reply = None
                query = update.get('callback_query')
                message = query.get('message', {}) if query else update.get('message', {})
                chat = message.get('chat', {})
                text = message.get('text','')
                acknowledgement = ''
                with db:
                    if not db.execute('SELECT 1 FROM updates WHERE id=?', (update['update_id'],)).fetchone():
                        if chat.get('type') in ('group','supergroup') and (not allowed or str(chat['id']) == allowed):
                            try:
                                if query:
                                    reply, acknowledgement = callback(db,query)
                                else:
                                    reply = handle(db,chat['id'],text,username,message.get('from'),message)
                            except ValueError as error:
                                reply = f'<h3>⚠️ Check that command</h3><p>{html.escape(str(error))}</p>'
                        elif chat.get('type') == 'private' and text.startswith('/'):
                            reply = '<p>Add me to your FC group to record games and view standings.</p>' + HELP
                        db.execute('INSERT OR IGNORE INTO updates VALUES(?)', (update['update_id'],))
                        db.execute('DELETE FROM updates WHERE id < ?', (update['update_id']-1000,))
                if query:
                    try:
                        api('answerCallbackQuery', callback_query_id=query['id'], text=acknowledgement)
                        if reply:
                            api('editMessageReplyMarkup', chat_id=chat['id'], message_id=message['message_id'], reply_markup={'inline_keyboard': []})
                    except APIError as error:
                        logging.warning('Button acknowledgement failed: %s', error)
                if reply:
                    try:
                        sent = send_reply(api, chat['id'], reply, message.get('message_thread_id'))
                        if isinstance(reply,dict) and reply.get('_session'):
                            with db:
                                db.execute('UPDATE sessions SET prompt=? WHERE id=?', (sent['message_id'],reply['_session']))
                    except APIError as error:
                        logging.warning('Reply failed: %s; match data remains saved', error)
        except APIError as error:
            logging.warning('%s; retrying shortly',error)
            for _ in range(5):
                if not running:
                    break
                time.sleep(1)
    db.close()


if __name__ == '__main__':
    main()
