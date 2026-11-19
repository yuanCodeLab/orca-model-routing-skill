import sys,json,unittest,subprocess,copy
from pathlib import Path
import hermetic
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import quota
CONFIG = hermetic.read_routes()
NOW=1800000000
# Gemini quota parsing is tested on a browser rule that still carries gemini, independent of live routing.
GCONFIG=copy.deepcopy(CONFIG)
GCONFIG['tasks']['browser']=dict(CONFIG['tasks']['browser'],alternate='gemini')

def win(used,minutes):
 return dict(usedPercent=used,windowMinutes=minutes,resetsAt=(NOW+minutes*30)*1000)
def entry():
 return dict(status='ok',updatedAt=NOW*1000,session=win(20,300),weekly=win(100,10080),buckets=[dict(group='Gemini Models',name='Gemini Models · Weekly Limit Remaining',**win(41,10080)),dict(group='Gemini Models',name='Gemini Models · Five Hour Limit Remaining',**win(20,300)),dict(group='Claude and GPT models',**win(100,10080))],usageMetadata={'secret':'NEVER_KEEP'},accountEmail='NEVER_KEEP')
def run_with(e):
 def runner(*args,**kwargs):
  return subprocess.CompletedProcess(args,0,json.dumps({'result':{'rateLimits':{'antigravity':e,'codex':dict(status='ok',updatedAt=NOW*1000,session=win(30,300),weekly=win(40,10080))}}}).encode())
 return runner
class Tests(unittest.TestCase):
 def snapshot(self,e):return quota.fetch_snapshot(quota.validate_policy(CONFIG['quota_policy']),runner=run_with(e),clock=lambda:NOW)
 def test_gemini_not_blocked_by_claude_gpt(self):
  r=quota.select(GCONFIG,'browser','normal',explicit_profile='gemini',runner=run_with(entry()),clock=lambda:NOW)
  self.assertEqual(r['selection']['selected'],'gemini')
  self.assertEqual(r['candidates'][0]['windows']['weekly']['remaining_pp'],59)
 def test_both_gemini_windows_selected(self):
  p=self.snapshot(entry())['providers']['antigravity']
  self.assertEqual(p['session']['usedPercent'],20);self.assertEqual(p['weekly']['usedPercent'],41)
 def test_gemini_exhausted_still_blocked(self):
  e=entry();e['weekly']=win(0,10080);e['buckets'][0]['usedPercent']=100
  r=quota.select(GCONFIG,'browser','normal',explicit_profile='gemini',runner=run_with(e),clock=lambda:NOW)
  self.assertEqual(r['selection']['blocked_code'],'quota_insufficient')
 def test_missing_group_does_not_use_other_group(self):
  e=entry();e['buckets']=e['buckets'][2:]
  self.assertIsNone(self.snapshot(e)['providers']['antigravity']['weekly'])
 def test_duplicate_window_is_unknown(self):
  e=entry();e['buckets'].append(copy.deepcopy(e['buckets'][0]))
  self.assertIsNone(self.snapshot(e)['providers']['antigravity']['weekly'])
 def test_malformed_buckets_are_unknown(self):
  e=entry();e['buckets']={'unexpected':1}
  self.assertIsNone(self.snapshot(e)['providers']['antigravity']['weekly'])
 def test_other_providers_unchanged(self):
  p=self.snapshot(entry())['providers']['codex'];self.assertEqual(p['weekly']['usedPercent'],40)
 def test_sensitive_fields_not_retained(self):
  self.assertNotIn('NEVER_KEEP',json.dumps(self.snapshot(entry())))
if __name__=='__main__':unittest.main()
