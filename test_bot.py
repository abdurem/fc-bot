import unittest
import sqlite3
import tempfile
from pathlib import Path
from html.parser import HTMLParser
from unittest.mock import Mock
from bot import connect, handle, send_reply, callback


class TableParser(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.rows = []
        self.cell = None
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        if tag == 'tr':
            self.rows.append([])
        elif tag in ('td', 'th'):
            self.cell = ''

    def handle_data(self, data):
        if self.cell is not None:
            self.cell += data

    def handle_endtag(self, tag):
        if tag in ('td', 'th'):
            self.rows[-1].append(self.cell)
            self.cell = None


class LeagueTests(unittest.TestCase):
    def setUp(self):
        self.db = connect(':memory:')

    def tearDown(self):
        self.db.close()

    def command(self, text, chat=-1):
        with self.db:
            return handle(self.db, chat, text, 'fc_bot')

    def test_scoring_and_draw(self):
        self.command('/result @Ali 3 @Sam 1')
        self.command('/result Sam 2 Ali 2')
        table = self.command('/table')
        self.assertEqual(TableParser(table).rows[1:], [
            ['1', 'ali', '2', '1', '1', '0', '5', '3', '2', '4'],
            ['2', 'sam', '2', '0', '1', '1', '3', '5', '-2', '1'],
        ])

    def test_seasons_deletion_and_group_isolation(self):
        self.command('/result A 1 B 0')
        self.assertIn('not found', self.command('/delete 1', -2))
        self.assertIn('no games', self.command('/table', -2))
        self.command('/newseason')
        self.assertIn('no games', self.command('/table'))
        self.assertIn('a', self.command('/table 1'))
        self.command('/delete 1')
        self.assertIn('no games', self.command('/table 1'))

    def test_validation_and_addressing(self):
        for text in ['/result A -1 B 0', '/result A 1 a 2', '/result A x B 2', '/table 99', '/result A 1']:
            with self.assertRaises(ValueError):
                self.command(text)
        self.assertIsNone(self.command('/result@other_bot A 1 B 0'))
        self.assertIn('Saved', self.command('/result@fc_bot A 1 B 0'))

    def test_ranking(self):
        self.command('/result A 1 C 0')
        self.command('/result B 3 C 0')
        table = self.command('/table')
        self.assertEqual([r[1] for r in TableParser(table).rows[1:]], ['b', 'a', 'c'])

    def test_rich_message_transport(self):
        api = Mock()
        reply = self.command('/result A 2 B 1')
        send_reply(api, -1, reply, 42)
        api.assert_called_once_with('sendRichMessage', chat_id=-1, rich_message={'html': reply}, message_thread_id=42, disable_notification=True)
        self.assertIn('<table bordered striped compact>', reply)
        self.assertNotIn('Stats key and ranking', reply)
        self.assertNotIn('<pre>', reply)

    def test_recent_matches_have_ids_and_scores(self):
        self.command('/result A 2 B 1')
        self.assertEqual(TableParser(self.command('/matches')).rows[1], ['#1', 'a', '2 – 1', 'b'])

    def test_plain_mentions_record_a_result(self):
        self.assertIn('Saved', self.command(' @Ali 3 @Sam 1 '))
        self.assertEqual(TableParser(self.command('/matches')).rows[1], ['#1', 'ali', '3 – 1', 'sam'])

    def test_ordinary_chat_does_not_record_games(self):
        for text in ['', 'hello', 'Ali 3 Sam 1', 'I predict @ali 3 @sam 1', '@ali 3 @sam 1 tomorrow']:
            self.assertIsNone(self.command(text))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM matches').fetchone()[0], 0)

    def test_plain_results_keep_score_validation(self):
        for text in ['@ali -1 @sam 2', '@ali 1000 @sam 0', '@ali 1 @ALI 2']:
            with self.assertRaises(ValueError):
                self.command(text)


class InteractiveTests(unittest.TestCase):
    def setUp(self):
        self.db = connect(':memory:')
        self.ali = {'id': 11, 'first_name': 'Ali', 'username': 'ali'}
        self.sam = {'id': 22, 'first_name': 'Sam', 'username': 'sam'}
        for user in (self.ali,self.sam):
            self.say('/join', user)

    def tearDown(self):
        self.db.close()

    def say(self, text, user=None, reply_to=None):
        message = {'message_thread_id': 4}
        if reply_to is not None:
            message['reply_to_message'] = {'message_id': reply_to}
        with self.db:
            return handle(self.db,-1,text,'fc_bot',user or self.ali,message)

    def tap(self, data, user=None, chat=-1):
        with self.db:
            return callback(self.db,{'data': data, 'from': user or self.ali,
                                     'message': {'chat': {'id': chat}, 'message_id': 100}})

    def select_players(self):
        first = self.say('/result')
        first_button = first['reply_markup']['inline_keyboard'][0][0]['callback_data']
        second, _ = self.tap(first_button)
        second_button = second['reply_markup']['inline_keyboard'][0][0]['callback_data']
        prompt, _ = self.tap(second_button)
        self.db.execute('UPDATE sessions SET prompt=200 WHERE id=?', (prompt['_session'],))
        return first_button, second_button, prompt

    def test_complete_flow_and_repeated_score(self):
        _, _, prompt = self.select_players()
        self.assertIn('Ali vs Sam', prompt['text'])
        self.assertIsNone(self.say('3 1', reply_to=199))
        self.assertIsNone(self.say('3 1', self.sam, reply_to=200))
        saved = self.say('3 1', reply_to=200)
        self.assertIn('Saved',saved)
        self.assertEqual(TableParser(self.say('/matches')).rows[1], ['#1','Ali','3 – 1','Sam'])
        self.assertIsNone(self.say('3 1', reply_to=200))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM matches').fetchone()[0],1)

    def test_callback_owner_stale_and_group_checks(self):
        first = self.say('/result')['reply_markup']['inline_keyboard'][0][0]['callback_data']
        self.assertIn('someone else', self.tap(first,self.sam)[1])
        self.assertIn('expired', self.tap(first,chat=-2)[1])
        self.tap(first)
        self.assertIn('already handled',self.tap(first)[1])
        self.say('/cancel')
        self.assertIn('expired',self.tap(first)[1])

    def test_invalid_score_then_retry_and_season_change(self):
        self.select_players()
        self.assertIn('two scores',self.say('1000 1',reply_to=200)['text'])
        self.assertIn('Saved',self.say('3-1',reply_to=200))
        self.select_players()
        self.say('/newseason')
        self.assertIn('season changed',self.say('3 1',reply_to=200))

    def test_identity_survives_username_and_name_change(self):
        self.select_players()
        self.say('2 2',reply_to=200)
        self.say('/join', {'id':11,'first_name':'Ali <legend>','username':'new_ali'})
        self.say('/result @new_ali 1 @sam 0')
        rows = TableParser(self.say('/table')).rows
        self.assertEqual(len(rows),3)
        self.assertEqual(rows[1][1:3],['Ali <legend>','2'])
        self.assertIn('Ali &lt;legend&gt;',self.say('/table'))

    def test_join_preserves_previous_username_results(self):
        self.say('/result newguy 2 sam 0')
        self.say('/join',{'id':33,'first_name':'New Guy','username':'newguy'})
        self.assertEqual(self.db.execute('SELECT home FROM matches').fetchone()[0],'tg:33')
        self.say('/join',{'id':33,'first_name':'New Guy','username':'newguy'})
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM players').fetchone()[0],3)

    def test_prompt_transport_omits_internal_session(self):
        _, _, prompt = self.select_players()
        api = Mock()
        send_reply(api,-1,prompt,4)
        kwargs = api.call_args.kwargs
        self.assertNotIn('_session',kwargs)
        self.assertEqual(api.call_args.args,('sendMessage',))
        self.assertTrue(kwargs['reply_markup']['force_reply'])
        self.assertTrue(kwargs['disable_notification'])

    def test_parallel_entries_and_restart(self):
        self.select_players()
        sam_entry = self.say('/result',self.sam)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0],2)
        self.say('/cancel',self.sam)
        self.assertIn('Saved',self.say('0:0',reply_to=200))
        stale_button = sam_entry['reply_markup']['inline_keyboard'][0][0]['callback_data']
        self.assertIn('expired',self.tap(stale_button,self.sam)[1])

    def test_too_few_players_and_expiration(self):
        self.db.execute('DELETE FROM players WHERE user=22')
        self.assertIn('two players',self.say('/result'))
        self.say('/join',self.sam)
        first = self.say('/result')['reply_markup']['inline_keyboard'][0][0]['callback_data']
        self.db.execute('UPDATE sessions SET created=0')
        self.assertIn('expired',self.tap(first)[1])

    def compare_players(self):
        first = self.say('/head2head')
        self.assertIn('Head to head',first['text'])
        button = first['reply_markup']['inline_keyboard'][0][0]['callback_data']
        second, _ = self.tap(button)
        second_button = second['reply_markup']['inline_keyboard'][0][0]['callback_data']
        result, _ = self.tap(second_button)
        return result, second_button

    def test_head2head_counts_both_player_orders(self):
        self.say('/result @ali 3 @sam 1')
        self.say('/result @sam 2 @ali 0')
        self.say('/result @ali 2 @sam 2')
        self.say('/result @ali 9 third 0')
        handle(self.db,-2,'/result ali 7 sam 0','fc_bot')
        self.db.execute("INSERT INTO matches(chat,season,home,away,hg,ag) VALUES(-1,2,'tg:11','tg:22',9,0)")
        before = self.db.execute('SELECT COUNT(*) FROM matches').fetchone()[0]
        result, button = self.compare_players()
        rows = TableParser(result).rows
        self.assertEqual(rows[1:5], [
            ['Wins','1','1'], ['Draws','1','1'], ['Goals','5','5'], ['Points','4','4']])
        self.assertEqual(rows[6:], [['#3','2','2'],['#2','0','2'],['#1','3','1']])
        self.assertIn('All square',result)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM matches').fetchone()[0],before)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0],0)
        self.assertIn('expired',self.tap(button)[1])

    def test_head2head_empty_and_leader(self):
        result, _ = self.compare_players()
        self.assertIn('No games between',result)
        self.say('/result @sam 4 @ali 1')
        result, _ = self.compare_players()
        self.assertIn('Sam leads on wins',result)
        self.say('/newseason')
        result, _ = self.compare_players()
        self.assertIn('No games between',result)
        self.assertIn('Season 2',result)

    def test_head2head_recent_meetings_limit(self):
        for _ in range(12):
            self.say('/result @ali 1 @sam 0')
        result, _ = self.compare_players()
        self.assertIn('12 games',result)
        rows = TableParser(result).rows
        self.assertEqual(rows[1],['Wins','12','0'])
        self.assertEqual(len(rows[6:]),10)


class MigrationTests(unittest.TestCase):
    def test_existing_sessions_default_to_result(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'league.sqlite3'
            db = sqlite3.connect(path)
            db.executescript('''CREATE TABLE sessions(id TEXT PRIMARY KEY, chat INTEGER, owner INTEGER,
                season INTEGER, thread INTEGER, home TEXT, away TEXT, prompt INTEGER, created INTEGER NOT NULL);
                INSERT INTO sessions(id,created) VALUES('old',0);''')
            db.close()
            db = connect(path)
            self.assertEqual(db.execute('SELECT mode FROM sessions').fetchone()[0],'result')
            db.close()
            db = connect(path)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0],1)
            db.close()


if __name__ == '__main__':
    unittest.main()
