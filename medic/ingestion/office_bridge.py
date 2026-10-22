"""Runs only with the pinned LibreOffice Python. UNO uses a private named pipe."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

import uno
import unohelper
from com.sun.star.task import XInteractionHandler


class AbortInteractions(unohelper.Base,XInteractionHandler):
    def handle(self,request):
        for continuation in request.getContinuations():
            abort=continuation.queryInterface(uno.getTypeByName('com.sun.star.task.XInteractionAbort'))
            if abort is not None: abort.select();return


def prop(name,value):
    result=uno.createUnoStruct('com.sun.star.beans.PropertyValue');result.Name=name;result.Value=value;return result


def connect(root,output):
    profile=output/'profile';(profile/'user').mkdir(parents=True)
    (profile/'user/registrymodifications.xcu').write_text('''<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry" xmlns:xs="http://www.w3.org/2001/XMLSchema"><item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop><prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item><item oor:path="/org.openoffice.Office.Jobs/Jobs/UpdateCheck/Arguments"><prop oor:name="AutoCheckEnabled" oor:op="fuse" oor:type="xs:boolean"><value>false</value></prop></item></oor:items>''',encoding='utf-8')
    pipe='medic_'+uuid.uuid4().hex
    process=subprocess.Popen([str(root/'program/soffice.exe'),'-env:UserInstallation='+profile.as_uri(),'--headless','--nologo','--nodefault','--norestore','--accept=pipe,name='+pipe+';urp;StarOffice.ServiceManager'],
        cwd=output,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
    context=uno.getComponentContext();resolver=context.ServiceManager.createInstanceWithContext('com.sun.star.bridge.UnoUrlResolver',context)
    deadline=time.monotonic()+20
    while time.monotonic()<deadline:
        try:
            remote=resolver.resolve('uno:pipe,name='+pipe+';urp;StarOffice.ComponentContext')
        except Exception:time.sleep(.1);continue
        provider=remote.ServiceManager.createInstanceWithContext('com.sun.star.configuration.ConfigurationProvider',remote)
        for path,key,expected in [('/org.openoffice.Office.Common/Security/Scripting','DisableMacrosExecution',True),('/org.openoffice.Office.Jobs/Jobs/UpdateCheck/Arguments','AutoCheckEnabled',False)]:
            configuration=provider.createInstanceWithArguments('com.sun.star.configuration.ConfigurationAccess',(prop('nodepath',path),))
            if configuration.getPropertyValue(key)!=expected:
                process.kill();raise RuntimeError('office_configuration_rejected')
        desktop=remote.ServiceManager.createInstanceWithContext('com.sun.star.frame.Desktop',remote)
        return process,desktop
    process.kill();raise RuntimeError('office_connect_timeout')


def main():
    request=json.loads(sys.stdin.buffer.read(16384));output=Path(request['output']);root=Path(request['engine_root'])
    process,desktop=connect(root,output);document=None
    try:
        filters={'docx':('Office Open XML Text','writer_pdf_Export'),'pptx':('Impress MS PowerPoint 2007 XML','impress_pdf_Export'),'xlsx':('Calc MS Excel 2007 XML','calc_pdf_Export')}
        input_filter,output_filter=filters[request['kind']]
        document=desktop.loadComponentFromURL(Path(request['path']).as_uri(),'_blank',0,tuple(prop(k,v) for k,v in {
            'Hidden':True,'ReadOnly':True,'Silent':True,'MacroExecutionMode':0,'UpdateDocMode':0,
            'InteractionHandler':AbortInteractions(),'FilterName':input_filter}.items()))
        if document is None:raise RuntimeError('office_load_rejected')
        if request['kind']=='xlsx': document.enableAutomaticCalculation(False)
        export=tuple(prop(k,v) for k,v in {'ExportNotes':False,'ExportBookmarks':False,'ExportFormFields':False,'ExportHiddenSlides':True}.items())
        document.storeToURL((output/'converted.pdf').as_uri(),(prop('FilterName',output_filter),prop('FilterData',uno.Any('[]com.sun.star.beans.PropertyValue',export)),prop('Overwrite',False)))
    finally:
        if document is not None:document.close(True)
        desktop.terminate()
        try:process.wait(timeout=3)
        except subprocess.TimeoutExpired:process.kill()


if __name__=='__main__':main()
