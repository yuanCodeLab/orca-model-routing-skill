import sys,json,unittest,subprocess
from pathlib import Path
import hermetic
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import quota
CONFIG = hermetic.read_routes()
NOW=1800000000

def win(used,minutes):
 return dict(usedPercent=used,windowMinutes=minutes,resetsAt=(NOW+minutes*30)*1000)
def run_with(fable):
 claude=dict(status='ok',updatedAt=NOW*1000,session=win(10,300),weekly=win(20,10080))
 if fable is not None:
  claude['fableWeekly']=fable
 def runner(*args,**kwargs):
  return subprocess.CompletedProcess(args,0,json.dumps({'result':{'rateLimits':{'claude':claude}}}).encode())
 return runner
def pick(kind,profile,fable):
 return quota.select(CONFIG,kind,'normal',explicit_profile=profile,runner=run_with(fable),clock=lambda:NOW)

class Tests(unittest.TestCase):
 def test_escalation_selectable_with_multiplied_budget(self):
  r=pick('complex','fable_high',win(0,10080))
  self.assertEqual(r['selection']['selected'],'fable_high')
  c=r['candidates'][0]
  self.assertEqual(c['role'],'escalation')
  self.assertEqual(c['windows']['weekly']['budget_pp'],CONFIG['quota_policy']['task_budget_points']['normal']['weekly']*CONFIG['models']['fable_high']['budget_multiplier'])
  self.assertIn('fableWeekly',c['caps'])
 def test_fable_cap_exhausted_blocks(self):
  r=pick('architecture','fable_high',win(99,10080))
  self.assertIsNone(r['selection']['selected'])
  self.assertEqual(r['candidates'][0]['status'],'insufficient')
 def test_fable_cap_missing_is_unknown(self):
  r=pick('complex','fable_high',None)
  self.assertIsNone(r['selection']['selected'])
  self.assertEqual(r['candidates'][0]['status'],'unknown')
 def test_not_escalation_for_other_tasks(self):
  with self.assertRaises(ValueError):
   pick('bugfix','fable_high',win(0,10080))
 def test_never_auto_selected(self):
  r=quota.select(CONFIG,'complex','normal',runner=run_with(win(0,10080)),clock=lambda:NOW)
  self.assertNotIn('fable_high',[c['profile'] for c in r['candidates']])

if __name__=='__main__':
 unittest.main()
