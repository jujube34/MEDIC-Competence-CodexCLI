"""Exercise browser aggregation on real JS, including missing and non-scoring data."""
import shutil
import subprocess
import unittest
from pathlib import Path


class ResultMetrics(unittest.TestCase):
    def test_ordinal_coverage_never_imputes_or_averages_partial_dimensions(self):
        node=shutil.which('node')
        if not node:
            self.skipTest('Node.js required to execute browser aggregation')
        root=Path(__file__).resolve().parents[2]
        script=r"""
const assert=require('node:assert/strict');
const {resultMetrics}=require('./medic/apps/web/results.js');
const framework='M1 M2 M3 M4 E1 E2 E3 E4 D1 D2 D3 I1 I2 C1 C2 C3 C4'.split(' ');
const row=(code,grade,ai=null,recommendation='C')=>({competency_id:code,recommendation,
  ai:{grade:ai},human:grade===undefined?null:{decision:{grade,disposition:grade===null?'insufficient_evidence':'assessed'}}});
let result=resultMetrics([row('M2','D','B'),row('C1',null),row('E2',undefined)],framework);
assert.equal(result.scope,3);assert.equal(result.total,17);assert.equal(result.human,1);
assert.equal(result.processed,2);assert.equal(result.ai,1);assert.equal(result.comparable,1);
assert.equal(result.met,1);assert.equal(result.gap,0);
assert.equal(result.dimensions[0].humanCount,1);assert.equal(result.dimensions[0].total,4);
assert.equal(result.dimensions[0].human,null);assert.equal(result.dimensions[0].recommendation,null);
assert.equal(result.items.find(i=>i.code==='M1').human,null);
assert.equal(result.items.find(i=>i.code==='C1').human,null);
assert.equal(result.items.find(i=>i.code==='E2').ai,null);
result=resultMetrics(['A','B','C','D'].map((grade,i)=>row('M'+(i+1),grade,grade)),framework);
assert.equal(result.dimensions[0].human,2.5);assert.equal(result.dimensions[0].ai,2.5);
assert.equal(result.dimensions[0].recommendation,3);assert.equal(result.gap,2);assert.equal(result.met,2);
assert.equal(result.dimensions[1].human,null);
result=resultMetrics([row('M2','',null)],framework);
assert.equal(result.human,0);assert.equal(result.gap,0);assert.equal(result.dimensions[0].human,null);
result=resultMetrics([],framework);assert.equal(result.scope,0);assert.equal(result.dimensions[4].human,null);
"""
        result=subprocess.run([node,'-e',script],cwd=root,text=True,capture_output=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
