"""Operator JSON and migration from the actual stale AMP selector schema."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from amp import production, map_discovery

ROOT = Path(__file__).resolve().parents[2]

class ReadablePresetTests(unittest.TestCase):
    def test_readable_migration_keeps_gameplay_and_removes_generated_metadata(self):
        doc=json.loads((ROOT/'src/runtime/curated_map_presets.json').read_text())
        original=copy.deepcopy(doc)
        readable=map_discovery.operator_presets(doc)
        self.assertEqual(doc,original)
        self.assertNotIn('capabilities',readable)
        for before,after in zip(doc['presets'],readable['presets']):
            expected=copy.deepcopy(before);expected.get('ai',{}).pop('capability',None)
            self.assertEqual(after,expected)
        self.assertEqual(map_discovery.operator_presets(readable),readable)

    def test_unusual_starter_only_legacy_approval_is_retained(self):
        doc={'version':1,'presets':[{'id':'manual','enabled':True}],
             'capabilities':[{'features':['ai_start'],'review':'legacy explicit review'}]}
        self.assertEqual(map_discovery.operator_presets(doc),doc)

    def test_staged_template_replaces_legacy_selector_and_preserves_rotation_and_foreign_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent=Path(tmp);root=parent/'war3-production';root.mkdir(mode=0o700)
            (root/'control').mkdir(parents=True,mode=0o700)
            (root/'data/config').mkdir(parents=True,mode=0o700)
            doc={'version':1,'presets':[{'id':'one','enabled':True,'map_filename':'One.w3x'},
                                      {'id':'two','enabled':True,'map_filename':'Two.w3x'}]}
            production.private_json(root/production.PRESETS,doc)
            settings={**production.DEFAULTS,'map_cycle':['one','two','one'],'max_concurrent_matches':3}
            production.private_json(root/'data/config/amp-settings.json',settings)
            template=json.loads((ROOT/'amp/war3-productionconfig.json').read_text())
            production.private_json(root/production.PRESET_MANIFEST,template)
            foreign={'ParamFieldName':'foreign','Category':'Other','InputType':'text','DefaultValue':'keep'}
            old=[{'ParamFieldName':'map_preset','Category':'War3_Dedicated:sports_esports','InputType':'enum','EnumValues':{'manual':'Manual'}},foreign]
            targets=[parent/'war3-productionconfig.json',parent/'configmanifest.json']
            for target in targets:production.private_json(target,old)
            before=(root/'data/config/amp-settings.json').read_bytes()
            with production.preset_manifest_update(root):pass
            for target in targets:
                current=json.loads(target.read_text())
                self.assertIn(foreign,current)
                self.assertFalse(any(f.get('ParamFieldName')=='map_preset' for f in current))
                cycle=[f for f in current if f.get('ParamFieldName')=='map_cycle']
                self.assertEqual(len(cycle),1);self.assertEqual(cycle[0]['InputType'],'Textarea')
                self.assertEqual(json.loads(cycle[0]['DefaultValue']),['one','two','one'])
            self.assertEqual((root/'data/config/amp-settings.json').read_bytes(),before)
            snapshots=[p.read_bytes() for p in targets]
            with production.preset_manifest_update(root):pass
            self.assertEqual([p.read_bytes() for p in targets],snapshots)

if __name__=='__main__':unittest.main()
