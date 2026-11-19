import sys,json,copy,unittest,subprocess
from pathlib import Path
import hermetic
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import quota
CONFIG = hermetic.read_routes()
NOW=1800000000

def win(used,minutes,elapsed_share=0.5):
 return dict(usedPercent=used,windowMinutes=minutes,resetsAt=int((NOW+minutes*60*(1-elapsed_share))*1000))
def run_with(codex_weekly_used,claude_weekly_used):
 def pool(used):
  return dict(status='ok',updatedAt=NOW*1000,session=win(10,300),weekly=win(used,10080))
 def runner(*args,**kwargs):
  return subprocess.CompletedProcess(args,0,json.dumps({'result':{'rateLimits':{
   'codex':pool(codex_weekly_used),'claude':pool(claude_weekly_used)}}}).encode())
 return runner
def pick(kind,codex_used,claude_used,config=None):
 return quota.select(config or CONFIG,kind,'normal',runner=run_with(codex_used,claude_used),clock=lambda:NOW)['selection']
def without_pool_override():
 # Remove the fixture's per-pool override to exercise the default threshold.
 config=copy.deepcopy(CONFIG)
 config['quota_policy']['primary_first'].pop('min_weekly_pace_ratio_by_pool',None)
 return config

class Tests(unittest.TestCase):
 def test_primary_kept_when_its_pool_is_behind_schedule(self):
  # Half the window elapsed, Codex only 20% used: pace > 1, Sol stays although Claude scores higher.
  sel=pick('feature',20,10)
  self.assertEqual(sel['selected'],CONFIG['tasks']['feature']['primary'])
  self.assertIn('主选优先',sel['reason'])
 def test_score_decides_when_primary_pool_is_overspending(self):
  # Half the window elapsed, Codex 80% used: pace < 1, fall back to score comparison.
  sel=pick('feature',80,10,without_pool_override())
  self.assertEqual(sel['selected'],CONFIG['tasks']['feature']['alternate'])
  self.assertNotIn('主选优先',sel['reason'])

if __name__=='__main__':
 unittest.main()
