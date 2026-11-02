import json
import unittest

from src.runtime.native import native_state


class NativeGenerationTests(unittest.TestCase):
    def test_optional_generation_is_typed_and_preserved(self):
        state = {'version': 1, 'phase': 'IDLE'}
        parse = lambda: native_state('[HOSTBOT_STATE] ' + json.dumps(state))
        self.assertNotIn('session_generation', parse())
        state['session_generation'] = 'a' * 32
        self.assertEqual(parse()['session_generation'], 'a' * 32)
        for bad in ('A' * 32, 'b' * 31, '', None, 42):
            state['session_generation'] = bad
            with self.subTest(value=bad), self.assertRaises(ValueError):
                parse()


    def test_catalog_proof_is_atomic_and_typed(self):
        state={'version':1,'phase':'LOBBY','session_generation':'a'*32,
               'host_counter':7,'catalog_request_id':'b'*32,'catalog_index':1}
        def parse():return native_state('[HOSTBOT_STATE] '+json.dumps(state))
        self.assertEqual(parse()['catalog_request_id'],'b'*32)
        for key,value in (('catalog_request_id','B'*32),('catalog_index',True),
                          ('catalog_index',32),('host_counter',0),('phase','IDLE'),
                          ('session_generation','bad')):
            old=state[key];state[key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):parse()
            state[key]=old
        del state['catalog_index']
        with self.assertRaises(ValueError):parse()

class NativeCatalogIdentityTests(unittest.TestCase):
    def setUp(self):
        self.state={'version':1,'phase':'LOBBY','session_generation':'a'*32,
                    'catalog_request_id':'b'*32,'catalog_index':0,'host_counter':7}
        self.expected={'generation':'a'*32,'request_id':'b'*32,'catalog_index':0,'host_counter':7}

    def parse(self, value=None):
        return native_state('[HOSTBOT_STATE] '+json.dumps(self.state if value is None else value))

    def test_boundaries_and_phases(self):
        for phase in ('LOBBY','COUNTDOWN','IN_GAME'):
            for index in (0,31):
                value={**self.state,'phase':phase,'catalog_index':index,'host_counter':2**28-1}
                parsed=self.parse(value)
                self.assertEqual((parsed['catalog_index'],parsed['host_counter']),(index,2**28-1))
        for counter in (0,-1,2**28,True,None):
            with self.subTest(counter=counter), self.assertRaises(ValueError):
                self.parse({**self.state,'host_counter':counter})
        for value in (-1,32,True,None,'1'):
            with self.subTest(index=value), self.assertRaises(ValueError):
                self.parse({**self.state,'catalog_index':value})
        for value in ('A'*32,'a'*31,'g'*32,'',None,123):
            with self.subTest(request=value), self.assertRaises(ValueError):
                self.parse({**self.state,'catalog_request_id':value})
        for key in ('session_generation','catalog_index','catalog_request_id','host_counter'):
            value=dict(self.state);del value[key]
            with self.subTest(missing=key), self.assertRaises(ValueError): self.parse(value)
        for phase in ('IDLE',None,[]):
            with self.subTest(phase=phase), self.assertRaises(ValueError):
                self.parse({**self.state,'phase':phase})
        with self.assertRaises(ValueError): self.parse({**self.state,'loading':True})
        self.assertTrue(self.parse({**self.state,'phase':'IN_GAME','loading':True})['loading'])

    def test_duplicate_fields_rejected_even_if_last_value_looks_valid(self):
        for key, last in (('catalog_request_id','"'+('b'*32)+'"'),
                          ('catalog_index','0'),('host_counter','7'),
                          ('session_generation','"'+('a'*32)+'"')):
            body=json.dumps(self.state)[:-1]+',"'+key+'":'+last+'}'
            with self.subTest(key=key), self.assertRaises(ValueError):
                native_state('[HOSTBOT_STATE] '+body)

    def test_stale_identity_and_started_game_never_match_current_selection(self):
        from src.runtime.native import native_catalog_lobby_matches
        parsed=self.parse()
        self.assertTrue(native_catalog_lobby_matches(parsed,**self.expected))
        for change in ({'generation':'c'*32},{'request_id':'d'*32},
                       {'catalog_index':1},{'host_counter':8},{'host_counter':True}):
            with self.subTest(change=change):
                self.assertFalse(native_catalog_lobby_matches(parsed,**{**self.expected,**change}))
        self.assertFalse(native_catalog_lobby_matches(self.parse({**self.state,'phase':'IN_GAME'}),**self.expected))
        self.assertTrue(native_catalog_lobby_matches(self.parse({**self.state,'phase':'COUNTDOWN'}),**self.expected))
        self.assertFalse(native_catalog_lobby_matches(self.parse({'version':1,'phase':'IDLE'}),**self.expected))


if __name__ == '__main__':
    unittest.main()
