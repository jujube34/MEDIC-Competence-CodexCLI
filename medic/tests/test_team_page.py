"""Filtering and page boundaries using the actual browser projection."""
import shutil
import subprocess
import unittest
from pathlib import Path


class TeamPage(unittest.TestCase):
    def test_latest_period_filters_stable_pages_and_empty_results(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node.js required')
        script = r"""
const assert=require('node:assert/strict');
const {teamPage}=require('./medic/apps/web/navigation.js');
const employees=Array.from({length:23},(_,i)=>({employee_ref:'SYN-'+(i+1),job_key:i%2?'医学顾问':'医学经理'}));
const records=[{employee_ref:'SYN-1',period:'2025',human_status:'finalized'},
 {employee_ref:'SYN-1',period:'2026',human_status:'pending'},
 {employee_ref:'SYN-2',period:'2026',human_status:'in_review'},
 {employee_ref:'OUTSIDE',period:'2026',human_status:'pending'}];
const before=JSON.stringify([employees,records]);
const pages=[1,2,3].map(page=>teamPage(employees,records,{page,size:10}));
assert.deepEqual(pages.map(p=>p.items.length),[10,10,3]);
assert.equal(new Set(pages.flatMap(p=>p.items.map(i=>i.employee.employee_ref))).size,23);
assert.equal(pages[1].items[0].employee.employee_ref,'SYN-11');
assert.deepEqual([pages[2].start,pages[2].end,pages[2].pages],[21,23,3]);
assert.equal(teamPage(employees,records,{status:'finalized'}).total,0);
let r=teamPage(employees,records,{period:'2025',status:'finalized',page:8,size:10});
assert.equal(r.total,1);assert.equal(r.page,1);assert.equal(r.items[0].latest.period,'2025');
r=teamPage(employees,records,{query:' sYn-2 ',job:'医学顾问',period:'2026',status:'not_submitted'});
assert.deepEqual(r.items.map(i=>i.employee.employee_ref),['SYN-20','SYN-22']);
r=teamPage(employees,records,{sort:'status',size:10});
assert.deepEqual(r.items.slice(0,2).map(i=>i.employee.employee_ref),['SYN-1','SYN-2']);
r=teamPage(employees,records,{query:'nothing',page:99,size:10});
assert.deepEqual([r.total,r.page,r.pages,r.start,r.end,r.items.length],[0,1,1,0,0,0]);
r=teamPage(employees.slice(0,2),records,{page:3,size:10});assert.equal(r.page,1);
assert.equal(teamPage(employees,records,{page:-1,size:0}).size,20);
assert.equal(JSON.stringify([employees,records]),before);
"""
        result = subprocess.run([node, '-e', script], cwd=Path(__file__).resolve().parents[2], text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
