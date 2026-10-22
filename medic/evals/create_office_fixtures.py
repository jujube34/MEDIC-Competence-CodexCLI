"""Run with the pinned LibreOffice Python; generate synthetic files only."""
import sys
sys.dont_write_bytecode=True
import os,argparse,tempfile
from pathlib import Path
os.environ['PYTHONDONTWRITEBYTECODE']='1'
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'ingestion'))
from office_bridge import connect,prop,uno
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--engine-root',required=True);parser.add_argument('--output',required=True)
args=parser.parse_args();root=Path(args.engine_root).resolve();output=Path(args.output).resolve();output.mkdir(parents=True)
with tempfile.TemporaryDirectory(prefix='medic-office-fixtures-') as temp:
 process,desktop=connect(root,Path(temp))
 try:
  doc=desktop.loadComponentFromURL('private:factory/swriter','_blank',0,(prop('Hidden',True),))
  doc.Text.String='Synthetic Office preview - not an employee evaluation.\n中文原件预览：内容、表格和分页验证。\nUnique evidence paragraph for source matching.'
  table=doc.createInstance('com.sun.star.text.TextTable');table.initialize(2,2)
  doc.Text.insertTextContent(doc.Text.End,table,False)
  for cell,text in [('A1','Item'),('B1','Evidence'),('A2','Synthetic'),('B2','Table content retained')]:table.getCellByName(cell).String=text
  doc.storeAsURL((output/'synthetic-office.docx').as_uri(),(prop('FilterName','Office Open XML Text'),));doc.close(True)
  doc=desktop.loadComponentFromURL('private:factory/simpress','_blank',0,(prop('Hidden',True),))
  pages=doc.getDrawPages()
  for i in range(2):
   slide=pages.getByIndex(0) if i==0 else pages.insertNewByIndex(i)
   shape=doc.createInstance('com.sun.star.drawing.TextShape');slide.add(shape)
   size=uno.createUnoStruct('com.sun.star.awt.Size');size.Width=20000;size.Height=7000;shape.setSize(size)
   pos=uno.createUnoStruct('com.sun.star.awt.Point');pos.X=1500;pos.Y=1500;shape.setPosition(pos)
   shape.String='Synthetic slide '+str(i+1)+'\nDistinct page evidence '+str(i+1);shape.CharHeight=24
  doc.storeAsURL((output/'synthetic-office.pptx').as_uri(),(prop('FilterName','Impress MS PowerPoint 2007 XML'),));doc.close(True)
  doc=desktop.loadComponentFromURL('private:factory/scalc','_blank',0,(prop('Hidden',True),))
  sheet=doc.Sheets.getByIndex(0);sheet.Name='Synthetic'
  for cell,text in [('A1','Synthetic worksheet'),('A2','First item'),('A3','Second item')]:sheet.getCellRangeByName(cell).String=text
  sheet.getCellRangeByName('B2').Value=10;sheet.getCellRangeByName('B3').Value=20;sheet.getCellRangeByName('B4').Formula='=SUM(B2:B3)'
  sheet.Columns.getByIndex(0).Width=6000
  doc.storeAsURL((output/'synthetic-office.xlsx').as_uri(),(prop('FilterName','Calc MS Excel 2007 XML'),));doc.close(True)
 finally:
  desktop.terminate();process.wait(timeout=5)
print('three synthetic Office fixtures saved')
