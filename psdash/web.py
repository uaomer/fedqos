# coding=utf-8
import logging
import psutil
import socket
import uuid
import locale
import os
import openpyxl
import re
import sqlite3
import pygal
import networkx as nx
import matplotlib.pyplot as plt 


import datetime
from datetime import datetime, timedelta

from openpyxl import load_workbook   
from openpyxl.compat import range
from openpyxl.cell import cell


from flask import Flask, render_template, request, session
from flask import jsonify, Response, Blueprint, current_app, g, flash, redirect, url_for
from flask_wtf import FlaskForm
from wtforms import Form,TextField,TextAreaField,validators,StringField,SubmitField, FileField, IntegerField
from werkzeug.local import LocalProxy
from werkzeug.utils import secure_filename 

from psdash.helpers import socket_families, socket_types
from fileinput import filename
from pyexcel.internal.sheets.row import Row
from eventlet.support.dns.rdatatype import NULL
from pip._vendor.html5lib.html5parser import method_decorator_metaclass
from sqlalchemy.dialects.postgresql.base import CIDR
from flask.helpers import flash
from keystoneclient.v3.contrib.trusts import Trust


ALLOWED_EXTENSIONS = set(['txt', 'pdf', 'png', 'jpg', 'jpeg', 'gif', 'xlsx'])
ALLOWED_ANSWER_YES = set(['yes', 'Yes', 'YES', 'YEs','yES','yeS', 'Y', 'y', 'X', 'x'])
ALLOWED_ANSWER_NO = set(['no', 'No', 'NO', 'nO', 'N', 'n', 'X', 'x'])
ALLOWED_ANSWER_NA = set(['na', 'Na', 'NA', 'nA', 'N.A', 'N.A.', 'n.a.', 'N.a.','n.A.', 'not applicable','Not Applicable', 'not Applicable', 'Not applicable', 'X', 'x'])

logger = logging.getLogger('psdash.web')
webapp = Blueprint('psdash', __name__, static_folder='static')
conn = sqlite3.connect('db.sqlite3',detect_types=sqlite3.PARSE_DECLTYPES)
cur = conn.cursor()

def get_current_node():
    return current_app.psdash.get_node(g.node)


def get_current_service():
    return get_current_node().get_service()


current_node = LocalProxy(get_current_node)
current_service = LocalProxy(get_current_service)


def fromtimestamp(value, dateformat='%Y-%m-%d %H:%M:%S'):
    dt = datetime.fromtimestamp(int(value))
    return dt.strftime(dateformat)


@webapp.context_processor
def inject_nodes():
    return {"current_node": current_node, "nodes": current_app.psdash.get_nodes()}


@webapp.context_processor
def inject_header_data():
    curtime = datetime.now()
    sysinfo = current_service.get_sysinfo()
    uptime = timedelta(seconds=sysinfo['uptime'])
    uptime = str(uptime).split('.')[0]
    return {
        'curtime':curtime,    
        'os': sysinfo['os'].decode('utf-8'),
        'hostname': sysinfo['hostname'].decode('utf-8'),
        'uptime': uptime
    }

@webapp.url_defaults
def add_node(endpoint, values):
    values.setdefault('node', g.node)


@webapp.before_request
def add_node():
    g.node = request.args.get('node', current_app.psdash.LOCAL_NODE)


@webapp.before_request
def check_access():
    if not current_node:
        return 'Unknown psdash node specified', 404

    allowed_remote_addrs = current_app.config.get('PSDASH_ALLOWED_REMOTE_ADDRESSES')
    if allowed_remote_addrs:
        if request.remote_addr not in allowed_remote_addrs:
            current_app.logger.info(
                'Returning 401 for client %s as address is not in allowed addresses.',
                request.remote_addr
            )
            current_app.logger.debug('Allowed addresses: %s', allowed_remote_addrs)
            return 'Access denied', 401

    username = current_app.config.get('PSDASH_AUTH_USERNAME')
    password = current_app.config.get('PSDASH_AUTH_PASSWORD')
    if username and password:
        auth = request.authorization
        if not auth or auth.username != username or auth.password != password:
            return Response(
                'Access deined',
                401,
                {'WWW-Authenticate': 'Basic realm="psDash login required"'}
            )


@webapp.before_request
def setup_client_id():
    if 'client_id' not in session:
        client_id = uuid.uuid4()
        current_app.logger.debug('Creating id for client: %s', client_id)
        session['client_id'] = client_id


@webapp.errorhandler(psutil.AccessDenied)
def access_denied(e):
    errmsg = 'Access denied to %s (pid %d).' % (e.name, e.pid)
    return render_template('error.html', error=errmsg), 401


@webapp.errorhandler(psutil.NoSuchProcess)
def access_denied(e):
    errmsg = 'No process with pid %d was found.' % e.pid
    return render_template('error.html', error=errmsg), 404


@webapp.route('/')
def index():
    sysinfo = current_service.get_sysinfo()
  
    netifs = current_service.get_network_interfaces().values()
    netifs.sort(key=lambda x: x.get('bytes_sent'), reverse=True)
  
    data = {
        'load_avg': sysinfo['load_avg'],
        'num_cpus': sysinfo['num_cpus'],
        'memory': current_service.get_memory(),
        'swap': current_service.get_swap_space(),
        'disks': current_service.get_disks(),
        'cpu': current_service.get_cpu(),
        'users': current_service.get_users(),
        'net_interfaces': netifs,
        'page': 'overview',
        'is_xhr': request.is_xhr
    }
  
    return render_template('index.html', **data)

@webapp.route('/processes', defaults={'sort': 'cpu_percent', 'order': 'desc', 'filter': 'user'})
@webapp.route('/processes/<string:sort>')
@webapp.route('/processes/<string:sort>/<string:order>')
@webapp.route('/processes/<string:sort>/<string:order>/<string:filter>')
def processes(sort='pid', order='asc', filter='user'):
    procs = current_service.get_process_list()
    num_procs = len(procs)

    user_procs = [p for p in procs if p['user'] != 'root']
    num_user_procs = len(user_procs)
    if filter == 'user':
        procs = user_procs

    procs.sort(
        key=lambda x: x.get(sort),
        reverse=True if order != 'asc' else False
    )

    
    return render_template(
        'processes.html',
        processes=procs,
        sort=sort,
        order=order,
        filter=filter,
        num_procs=num_procs,
        num_user_procs=num_user_procs,
        page='processes',
        is_xhr=request.is_xhr
    )


@webapp.route('/process/<int:pid>', defaults={'section': 'overview'})
@webapp.route('/process/<int:pid>/<string:section>')
def process(pid, section):
    valid_sections = [
        'overview',
        'threads',
        'files',
        'connections',
        'memory',
        'environment',
        'children',
        'limits'
    ]

    if section not in valid_sections:
        errmsg = 'Invalid subsection when trying to view process %d' % pid
        return render_template('error.html', error=errmsg), 404

    context = {
        'process': current_service.get_process(pid),
        'section': section,
        'page': 'processes',
        'is_xhr': request.is_xhr
    }

    if section == 'environment':
        penviron = current_service.get_process_environment(pid)

        whitelist = current_app.config.get('PSDASH_ENVIRON_WHITELIST')
        if whitelist:
            penviron = dict((k, v if k in whitelist else '*hidden by whitelist*') 
                             for k, v in penviron.iteritems())

        context['process_environ'] = penviron
    elif section == 'threads':
        context['threads'] = current_service.get_process_threads(pid)
    elif section == 'files':
        context['files'] = current_service.get_process_open_files(pid)
    elif section == 'connections':
        context['connections'] = current_service.get_process_connections(pid)
    elif section == 'memory':
        context['memory_maps'] = current_service.get_process_memory_maps(pid)
    elif section == 'children':
        context['children'] = current_service.get_process_children(pid)
    elif section == 'limits':
        context['limits'] = current_service.get_process_limits(pid)

    return render_template(
        'process/%s.html' % section,
        **context
    )


@webapp.route('/network')
def view_networks():
    netifs = current_service.get_network_interfaces().values()
    netifs.sort(key=lambda x: x.get('bytes_sent'), reverse=True)

    # {'key', 'default_value'}
    # An empty string means that no filtering will take place on that key
    form_keys = {
        'pid': '', 
        'family': socket_families[socket.AF_INET],
        'type': socket_types[socket.SOCK_STREAM],
        'state': 'LISTEN'
    }

    form_values = dict((k, request.args.get(k, default_val)) for k, default_val in form_keys.iteritems())

    for k in ('local_addr', 'remote_addr'):
        val = request.args.get(k, '')
        if ':' in val:
            host, port = val.rsplit(':', 1)
            form_values[k + '_host'] = host
            form_values[k + '_port'] = int(port)
        elif val:
            form_values[k + '_host'] = val

    conns = current_service.get_connections(form_values)
    conns.sort(key=lambda x: x['state'])

    states = [
        'ESTABLISHED', 'SYN_SENT', 'SYN_RECV',
        'FIN_WAIT1', 'FIN_WAIT2', 'TIME_WAIT',
        'CLOSE', 'CLOSE_WAIT', 'LAST_ACK',
        'LISTEN', 'CLOSING', 'NONE'
    ]

    return render_template(
        'network.html',
        page='network',
        network_interfaces=netifs,
        connections=conns,
        socket_families=socket_families,
        socket_types=socket_types,
        states=states,
        is_xhr=request.is_xhr,
        num_conns=len(conns),
        **form_values
    )


@webapp.route('/disks')
def view_disks():
    disks = current_service.get_disks(all_partitions=True)
    io_counters = current_service.get_disks_counters().items()
    io_counters.sort(key=lambda x: x[1]['read_count'], reverse=True)
    return render_template(
        'disks.html',
        page='disks',
        disks=disks,
        io_counters=io_counters,
        is_xhr=request.is_xhr
    )


@webapp.route('/logs')
def view_logs():
    available_logs = current_service.get_logs()
    available_logs.sort(cmp=lambda x1, x2: locale.strcoll(x1['path'], x2['path']))

    return render_template(
        'logs.html',
        page='logs',
        logs=available_logs,
        is_xhr=request.is_xhr
    )


@webapp.route('/log')
def view_log():
    filename = request.args['filename']
    seek_tail = request.args.get('seek_tail', '1') != '0'
    session_key = session.get('client_id')

    try:
        content = current_service.read_log(filename, session_key=session_key, seek_tail=seek_tail)
    except KeyError:
        error_msg = 'File not found. Only files passed through args are allowed.'
        if request.is_xhr:
            return error_msg
        return render_template('error.html', error=error_msg), 404

    if request.is_xhr:
        return content

    return render_template('log.html', content=content, filename=filename)


@webapp.route('/log/search')
def search_log():
    filename = request.args['filename']
    query_text = request.args['text']
    session_key = session.get('client_id')

    try:
        data = current_service.search_log(filename, query_text, session_key=session_key)
        return jsonify(data)
    except KeyError:
        return 'Could not find log file with given filename', 404


@webapp.route('/register')
def register_node():
    name = request.args['name']
    port = request.args['port']
    host = request.remote_addr

    current_app.psdash.register_node(name, host, port)
    return jsonify({'status': 'OK'})


# handling forms the flasky way 

class ReusableForm(Form):
    cid =  IntegerField('Id:', validators=[validators.required()])
    cname = TextField('Name:', validators=[validators.required()])
    cmeta = TextField('Metatext:', validators=[validators.required()])    
    cendpoint = TextField('Endpoint URL:', validators=[validators.required()])
    tlimit = TextField('Threshold:', validators=[validators.required()])
#     cinfo = TextField('Description:', validators=[validators.required()])
#     cinfo = TextField('Description:', validators=[validators.required()])
#     cinfo = TextField('Description:', validators=[validators.required()])
#     cinfo = TextField('Description:', validators=[validators.required()])
#     
 
@webapp.route("/signup", methods=['GET', 'POST'])
def signup():
    form = ReusableForm(request.form)
    print form.errors
    
    if request.method == 'POST':
        
        cname=request.form['cname']
        cmeta = request.form['cmeta']
        cendpoint = request.form['cendpoint']
        cinfo = request.form['cinfo']
        
            
        if form.validate():
            cur.execute("INSERT INTO cprofile(cname,cmeta,cendpoint,cinfo) VALUES (?,?,?,?)", (cname,cmeta,cendpoint,cinfo)  ) 
            conn.commit()
            flash('New cloud added to database successfully.') #Display a message to end user at front end.
            cur.execute("select * from cprofile where cname=?" , [(cname)]  )
            whois = cur.fetchone()
            if whois:
                print whois[0]
                print whois[1]
                session['svar_cid'] = whois[0]
                session['svar_cname'] = whois[1]
            
            return redirect('/upload')
        else:
            flash('All the form fields are required. ')
 
    return render_template('signup.html', form=form,is_xhr=request.is_xhr )

def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS
           
@webapp.route('/upload', methods=['GET', 'POST'])
def upload_file():
    if request.method == 'POST':
        # check if the post request has the file part
        
        if 'file' not in request.files:
            flash('No file part')
            return redirect(request.url)
        
        ufile = request.files['file']
        # if user does not select file, browser also
        # submit a empty part without filename
        if ufile.filename == '':
            flash('No selected file')
            print ('No selected file')
            return redirect(request.url)
        if not allowed_file(ufile.filename):
            print ('File type not permitted')
            return redirect(request.url)
        
        if ufile and allowed_file(ufile.filename):
            filename = secure_filename(ufile.filename)            
            svar_cname = session.get('svar_cname', None)
            svar_cid = session.get('svar_cid', None)
            ufile.save(os.path.join('/tmp/',svar_cname))
            
            wb = load_workbook(ufile)
            sheets = wb.get_sheet_names()
            print sheets
            
            for sheet in sheets:
                ws = wb[sheet] 
                print ws 
                
                maxm_row = ws.max_row
                print maxm_row
                maxm_col = ws.max_column
                print maxm_col
               
                for x in range (2,maxm_row+1):
                    #for y in range (1,maxm_col+1):
                    cgroup_id=ws.cell(row=x,column=1).value  
                    cont_id=ws.cell(row=x,column=2).value
                    ayes = ws.cell(row=x,column=3).value
                    ano = ws.cell(row=x,column=4).value
                    ana = ws.cell(row=x,column=5).value
                    
                    if ayes in ALLOWED_ANSWER_YES:
                            ans = '1'
                    elif ano in ALLOWED_ANSWER_NO:
                        ans = '2'      
                    elif ana in ALLOWED_ANSWER_NA: 
                        ans = '3'
                    else: 
                        ans = '4'
                    
                    cur.execute("INSERT INTO caiqanswer (control_id,choice_id, cloud_id,cgroup_id) VALUES (?,?,?,?)", (cont_id,ans,svar_cid,cgroup_id)  )
                    conn.commit()
                print 'success!!!!'
            flash('File upload success!!')
            
            cur.execute('select short_code from caiqcgroup ')
            group_dict = cur.fetchall()
            c_trust_eval=[]
            for group in group_dict: 
                print group[0]
                c_trust_eval.append (cgroup_score(svar_cid, group[0])) # Evaluating trust based on caiq for each control group 
            
            caiq_trust_score(svar_cid)
            
            return redirect('/login')
    return render_template('upload.html')


@webapp.route('/login')
def login(): 
    return render_template('login.html')

def slugify(text, lower=1):
    if lower == 1:
        text = text.strip().lower()
    text = re.sub(r'[^\w _-]+', '', text)
    text = re.sub(r'[- ]+', '_', text)
    return text



@webapp.route('/profiles', defaults={'sort': 'avg_e', 'order': 'desc'})
@webapp.route('/profiles/<string:sort>')
@webapp.route('/profiles/<string:sort>/<string:order>')
def profiles(sort='id', order='asc'):
    
    query1= "select id,cname,cendpoint,strftime('%s','now','localtime') - strftime('%s',lastseen) AS 'timesince',lastseen,avg_e from cprofile  order by " 
    query2 =  sort + ' ' + order
    #print query1+query2
    cur.execute (query1+query2)
    profiles = cur.fetchall()

    return render_template('profiles.html', profiles=profiles, sort=sort, order=order, page='profiles', is_xhr=request.is_xhr)

@webapp.route('/profile/<int:cid>', defaults={'section': 'overview'})
@webapp.route('/profile/<string:cid>', defaults={'section': 'overview'})
@webapp.route('/profile/<int:cid>/<string:section>')
def profile(cid, section):
    print "This is profile function", cid
    valid_sections = [
        'overview',
        'assessment',
        'resources',
        'trust',
        'graph',
        ]

    if section not in valid_sections:
        errmsg = 'Invalid subsection when trying to view detail' 
        return render_template('error.html', error=errmsg), 404
    
    
    # trust evaluation starts here - Note: this must move to file upload section
    c_trust_eval= []
    select_profile = profile_detail(cid) # profile details 
    print "This is cloud ", select_profile
    
    caiq_assessment_detail = caiq_detail(cid) # caiq details 
    print "This is CAIQ assessment", caiq_assessment_detail

    #cur.execute ('select ctrustinfo.*,caiqcgroup.* from ctrustinfo INNER Join caiqcgroup on ctrustinfo.cgroup_id=caiqcgroup.id and ctrustinfo.cloud_id=?', [(cid)])
    cur.execute ('select caiqcgroup.group_name,caiqcgroup.nofquestion,ctrustinfo.count_na, (caiqcgroup.nofquestion-ctrustinfo.count_na) AS "Total Applicable", ctrustinfo.count_yes,ctrustinfo.count_no,ctrustinfo.count_un,ctrustinfo.caiq_t,ctrustinfo.caiq_c,ctrustinfo.caiq_f,ctrustinfo.caiq_e  from ctrustinfo INNER Join caiqcgroup on ctrustinfo.cgroup_id=caiqcgroup.id and ctrustinfo.cloud_id=?', [(cid)])
    ctrust= cur.fetchall()
    
    cur.execute('select all_na, (295-all_na) AS "Total Applicable", all_yes,all_no,all_un,avg_t,avg_c,avg_f,avg_e from cprofile where id=?',[(cid)])
    misc_data = cur.fetchone()
    
#     view_trust_detail = cur.fetchall()
#     print "This is trust detail", view_trust_detail
#     misc_data = 1,2,3,4,5,6,7,8,9,10 # just a jugad
#     
    graph_data = draw_graph(cid)
    
    context = {
        'profile': select_profile,
        'caiq': caiq_assessment_detail,
        'ctrust':ctrust,
        'misc_data': misc_data,
        'graph_data': graph_data,
        'section': section,
        'page': 'profiles',
        'is_xhr': request.is_xhr
    }
    
    if section == 'assessment':

        context['assessment'] = 'This is profile assessment'
       
        
    elif section == 'resources':
        context['resources'] = 'This is resources panel'
       
    elif section == 'trust':

        context['trust'] = 'This is trust management'
       # print "This is full context= ", context

    elif section == 'graph':
    
        context['graph'] = 'This is graphical representation of trust'
      
 
    return render_template(
        'profile/%s.html' % section,
        **context
    )

    
def profile_detail(cid):
    
    cur.execute ("select * from cprofile where id=?", [cid])
    #cur.execute("select cprofile.*, TempTable.* from TempTable INNER Join cprofile on TempTable.cloud_id = cprofile.id and TempTable.cloud_id=?",[cid])
    whois = cur.fetchone()  
    if not whois:
        errmsg = 'Invalid profile when trying to view detail' 
        return render_template('error.html', error=errmsg), 404
    
    return whois

def caiq_detail(cid):
    
    cur.execute('select caiqanswer.cloud_id, caiqanswer.control_id, caiqquestion.q_text, caiqanswer.choice_id, caiqchoice.choice_text  from ((caiqanswer LEFT Join caiqquestion on caiqanswer.control_id=caiqquestion.id) inner join caiqchoice on caiqanswer.choice_id=caiqchoice.id) where cloud_id=?', [cid]) 
      
    caiq = cur.fetchall()
    
    if not caiq:
        print 'Nothing found'
        errmsg = 'Invalid profile when trying to view detail' 
        return render_template('error.html', error=errmsg), 404
    
    return caiq

def cgroup_score(cid,cgroup):
    
    print "this is caiq_funtion. I got CID=", cid, 'and cgroup=',cgroup
    cur.execute('select id,group_name,nofquestion from caiqcgroup where short_code like :1', ([cgroup]))
    result = cur.fetchone()
    cgroup_id = result[0]
    cgroup_name = result[1]
    count_total = result[2]
    print "This is group:", cgroup_name, "with ID: ", cgroup_id
    print "Total Question=", count_total
    
    cur.execute('select count(id) from caiqanswer where cloud_id=:1 and choice_id=:2 and cgroup_id=:3', (cid,'1',cgroup_id))
    result = cur.fetchone()
    count_yes = result[0]
    print 'Yes=     ', count_yes
    
    cur.execute('select count(id) from caiqanswer where cloud_id=:1 and choice_id=:2 and cgroup_id=:3', (cid,'2',cgroup_id)) 
    result = cur.fetchone()
    count_no = result[0]
    print 'No=      ', count_no
    
    cur.execute('select count(id) from caiqanswer where cloud_id=:1 and choice_id=:2 and cgroup_id=:3', (cid,'3',cgroup_id)) 
    result = cur.fetchone()
    count_na = result[0]
    print 'NA=      ', count_na
    
    cur.execute('select count(id) from caiqanswer where cloud_id=:1 and choice_id=:2 and cgroup_id=:3', (cid,'4',cgroup_id)) 
    result = cur.fetchone()
    count_un = result[0]
    print 'Unknown= ', count_un
    
    print '----------------------------'
    
    total_applicable = (count_no + count_un + count_yes)
    print "total applicable", total_applicable
    
#     sum_y_no = float(count_yes[0]+ count_no[0])
#     print sum_y_no
    
    if  (count_yes + count_no) == 0: 
        caiq_t=0
    else: 
        caiq_t = round(count_yes / (float(count_yes + count_no)),4)
        print "CAIQ T= ", caiq_t
    
#     temp1= ta_assessment * (yes_assessment + no_assessment)    
    
    if total_applicable==0:
        caiq_c = 1
        print "Total applicable was Zero so CAIQ_C= ",caiq_c 
    else:
        temp1 = total_applicable * (count_yes+count_no)
        print "temp1 =", temp1
        
    #     temp2 = 2*(ta_assessment - (yes_assessment+no_assessment))
        temp2 = 2*(total_applicable - (count_yes+count_no))
        print "temp2=", temp2
    #     trust_c= temp1 / (temp2+temp1)
        caiq_c = round(temp1 / float(temp2+temp1),4)
        print "-------------------------------"
        print "CAIQ_C= ", caiq_c

#     trust_f= 0.99
    caiq_f = 0.99
    
#     trust_e = trust_t * trust_c + (1-trust_c)*trust_f
    caiq_e = caiq_t * caiq_c + (1-caiq_c)*caiq_f
    print "CAIQ E Score" , caiq_e
    
    #caiq_t = round(count_yes / (float(count_yes + count_no)),4)
    
    caiq_b = round(count_yes / float(count_yes + count_no + 2),4)
    print caiq_b
    caiq_d = round(count_no / float(count_yes + count_no + 2),4)
    print caiq_d
    caiq_u = round(2 / float(count_yes + count_no + 2),4)
    print caiq_u

            
    cur.execute('INSERT INTO ctrustinfo(cgroup_id,cloud_id,caiq_t,caiq_c,caiq_e,caiq_f,caiq_b,caiq_d,caiq_u,count_yes,count_no,count_na,count_un) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)', (cgroup_id,cid,caiq_t,caiq_c,caiq_e,caiq_f,caiq_b,caiq_d, caiq_u,count_yes,count_no,count_na,count_un)) 
    conn.commit()
    
    
            
    #print total_applicable   
    return {'count_yes':count_yes,'count_no':count_no,'count_na':count_na,'count_un':count_un,'total_applicable': total_applicable, 'caiq_t': round(caiq_t,4), 'caiq_c':round(caiq_c,4),'caiq_e':round(caiq_e,4),'caiq_f':round(caiq_f,4), 'cgroup':cgroup_name,'count_total':count_total} 
    
@webapp.route('/edit', methods=['GET', 'POST'] )
#@webapp.route('/edit/<string:cid>' )
def edit_profile():
    
    if request.method == 'POST':
        
        if request.form['faction'] == 'new': 
            return redirect('/signup')
        
        if request.form['faction'] == 'composite':
            if 'cart' not in session:
                flash ("Nothing to do")
                return redirect("/profiles")
            else: 
                return redirect("/composite")
            
        
        if request.form['faction'] == 'compare':
            if 'cart' not in session:
                flash ("Nothing to compare")
                return redirect("/profiles")
            
            cloud_list=[]
            for cid in session.pop('cart', []):
                cloud_list.append(cid)
            print len(cloud_list)
            if len(cloud_list)==1: 
                flash("Can't compare single item") 
                return redirect("/profiles") 
             
            result_compare = caiq_compare(cloud_list).get('result_compare')
            cloud_detail = caiq_compare(cloud_list).get('cloud_detail')
            

             
            return render_template('compare.html', cloud_detail=cloud_detail, cloud_list=cloud_list, result_compare=result_compare) 
         
          
           # return "This page is underconstruction"
                
        #cid.append( request.form.getlist('cid'))
        if request.form['cid'] and request.form['faction'] == 'delete':
            cid = request.form['cid']
            faction = request.form['faction']
            
            print 'For delete=', faction, cid
                     
            cur.execute( 'delete from cprofile where id IN (:1)',([cid]))
            conn.commit()
            #msg='Profile deleted successfully'
            flash('Profile deleted successfully')
            print("affected rows = {}".format(cur.rowcount))
                 
                #         if not cid:
        #             print 'Nothing found'
        #             errmsg = 'Invalid profile when trying to view detail' 
        #             return render_template('error.html', error=errmsg), 404
        # #     
   # print 'All I got is', faction, 'and ', cid, 'and ', cname
   
    
    return redirect('/profiles')

#@webapp.route('/compare', methods=['GET', 'POST'] )
@webapp.route('/add_to_compare/<string:cid>' )
def add_to_compare(cid):
      
    
    if 'cart' in session:
        
       # print session['cart']
        
        if len(session['cart']) == 4:
           # print len(session['cart']), "Cart Full..cant add more"
            flash('Cart Full..Cant add more')
            exit

        else:
        #    print "Adding ", cid
            
            session['cart'].append(cid)
            flash(len(session['cart']))
         #   print "Now the cart is ", session['cart']
    else:
        session['cart'] = [cid]
        flash(len(session['cart']))
    #print len(session['cart'])
    return redirect( request.referrer)    
    
    #return redirect('/profiles')  

@webapp.route("/test/<string:cid>")
def draw_graph(cid):
    
    #Plotting all values for trust as graph
    graph = pygal.Line(range=(0,1.2 )) 
    graph.title = ' Trust Evaluation for each control '
    x_label = []
    
    cur.execute('select short_code from caiqcgroup ORDER BY id')
    groups = cur.fetchall()
    for group in groups: # graph labels 
        x_label.append(group[0])    
    graph.x_labels = x_label

    #select ctrustinfo.caiq_e,ctrustinfo.cgroup_id,caiqcgroup.group_name,caiqcgroup.short_code from ctrustinfo  inner join caiqcgroup on ctrustinfo.cgroup_id=caiqcgroup.id and ctrustinfo.cloud_id=51  
    mark_list =[]
    cur.execute('select ctrustinfo.caiq_e,ctrustinfo.cgroup_id,caiqcgroup.group_name,caiqcgroup.short_code from ctrustinfo inner join caiqcgroup on ctrustinfo.cgroup_id=caiqcgroup.id and ctrustinfo.cloud_id=? ORDER BY cgroup_id', [(cid)])
    c_trust_eval = cur.fetchall()
    for x in c_trust_eval:
        mark_list.append(x[0])
    avg_caiq_e = sum(mark_list)/16
    print mark_list, avg_caiq_e
    
    graph.add('CAIQ E-Score',mark_list)
    
    list_avg_e = [] # plotting average score as blue line against each dot .--.--.--.--.--. 
    for x in range(len(mark_list)):         
        list_avg_e.append(avg_caiq_e)          
      
    graph.add("CAIQ Avg Escore", list_avg_e, show_dots=False)
    graph_data = graph.render_data_uri()    # drawing the graph
# 
#     context = {
#         'ctrust':c_trust_eval,
#         'graph_data': graph_data,
#         'page': 'profiles',
#         'is_xhr': request.is_xhr
#     }
#  
#     return render_template(
#         'graphing.html' ,
#         **context
#     )
    return graph_data



def caiq_trust_score(cid): # its one time functionfor every profile. never call twice 
    
    cur.execute('select count(id) from caiqanswer where caiqanswer.cloud_id=:1 and caiqanswer.choice_id=1', [(cid)])
    sum_all_yes = cur.fetchone()
     
    cur.execute('select count(id) from caiqanswer where caiqanswer.cloud_id=:1 and caiqanswer.choice_id=2', [(cid)])
    sum_all_no = cur.fetchone()
    
    cur.execute('select count(id) from caiqanswer where caiqanswer.cloud_id=:1 and caiqanswer.choice_id=3', [(cid)])
    sum_all_na = cur.fetchone()
    
    cur.execute('select count(id) from caiqanswer where caiqanswer.cloud_id=:1 and caiqanswer.choice_id=4', [(cid)])
    sum_all_un = cur.fetchone()
    
    cur.execute('select  sum(caiq_t)/16 from ctrustinfo where cloud_id=:1', [(cid)])
    avg_caiq_t = cur.fetchone() 
    #avg_caiq_t = sum([x['caiq_t'] for x in c_trust_eval])/16  
    
    cur.execute('select  sum(caiq_c)/16 from ctrustinfo where cloud_id=:1', [(cid)])
    avg_caiq_c = cur.fetchone()
    #avg_caiq_c = sum([x['caiq_c'] for x in c_trust_eval])/16  
    
    cur.execute('select  sum(caiq_e)/16 from ctrustinfo where cloud_id=:1', [(cid)])
    avg_caiq_e = cur.fetchone()
    #avg_caiq_e = sum([x['caiq_e'] for x in c_trust_eval])/16   
    
    cur.execute('select  sum(caiq_f)/16 from ctrustinfo where cloud_id=:1', [(cid)])
    avg_caiq_f = cur.fetchone()
    #avg_caiq_f =  x['caiq_f'] * x['caiq_f']
    
    cur.execute('select  sum(caiq_b)/16 from ctrustinfo where cloud_id=:1', [(cid)])
    avg_caiq_b = cur.fetchone()
    cur.execute('select  sum(caiq_d)/16 from ctrustinfo where cloud_id=:1', [(cid)])
    avg_caiq_d = cur.fetchone()
    cur.execute('select  sum(caiq_u)/16 from ctrustinfo where cloud_id=:1', [(cid)])
    avg_caiq_u = cur.fetchone()
    
    total_applicable = 295-sum_all_na[0]
    
    
    misc_data = sum_all_yes,sum_all_no, sum_all_na, sum_all_un, avg_caiq_t, avg_caiq_c,  avg_caiq_f,avg_caiq_e, total_applicable, avg_caiq_b, avg_caiq_d,avg_caiq_u 
    
    cur.execute("update cprofile set all_yes=:1,all_no=:2,all_na=:3,all_un=:4, avg_t=:5, avg_c=:6, avg_e=:7, avg_f=:8,avg_b=:9,avg_d=:10,avg_u=:11 where cprofile.id=:12", (sum_all_yes[0],sum_all_no[0],sum_all_na[0],sum_all_un[0],avg_caiq_t[0],avg_caiq_c[0],avg_caiq_e[0],avg_caiq_f[0],avg_caiq_b[0],avg_caiq_d[0],avg_caiq_u[0],cid))
    conn.commit()
    
    return misc_data



@webapp.route('/composite')
def composite_trust():
    
#     # Note: trust values must scale in the range (distrust,uncertainity,trust) or 
    # between (strong distrust, weak distrust, uncertainity, weak trust, strong trust)
    cloud_list=[]
    parsed_result = []
    for cid in session.pop('cart', []):
        cloud_list.append(cid)
            
    result_compare = caiq_compare(cloud_list).get('result_compare')
    #print result_compare
    cloud_detail = caiq_compare(cloud_list).get('cloud_detail')
     
    print len(result_compare)
    for x in range(len(result_compare)):
        temp1=[]
        for y in range(len(result_compare[x])): 
            if y==0 or y==1:
                temp1.append(result_compare[x][y])
                continue
            
            temp1.append(parse_trust(result_compare[x][y]))

        
        print "This is temp", temp1
        #temp2 = temp1[2]+temp1[3]
        #print temp2
        temp1.append(find_and(temp1))
        
        print "This is temp after result and", temp1
        parsed_result.append(temp1)

    print "This is parsed result", parsed_result
       
#         
    return render_template("composite.html", parsed_result=parsed_result, cloud_detail=cloud_detail)
    #return "Calculate Probabiltiy that B is trust when A is trust "

def parse_trust(caiq_score):
    #print "This is trust parser. Got caiq score=",caiq_score
    
    # this is the point where a CSP will define its level of trust (May be we will see where the cloud score is itself)  
    
    if caiq_score > 0.5 and caiq_score<=1 : caiq_score='Trust' 
    elif caiq_score < 0.5 and caiq_score>=0 : caiq_score= 'Distrust' 
    elif caiq_score == 0.5: caiq_score='Uncertain'
    else: 
        caiq_score="Fake assessment"
    #print caiq_score
    sub_trust_value = caiq_score 
    return sub_trust_value

    #return "oki doki.this is trust parser"
    #return  
def caiq_compare(cloud_list):
    
#     print [str(x) for x in cloud_list]
#     print [x.encode('UTF8') for x in cloud_list]
# 
#     print "THis is cloud list ", cloud_list
#     all_cloud= cloud_list[0]+','+ cloud_list[1]
#     
#     print all_cloud
#  
    cloud_detail = []
    query_start= 'select ctrustinfo.cgroup_id, caiqcgroup.group_name,' 
    query_end= 'from ctrustinfo INNER JOIN caiqcgroup on ctrustinfo.cgroup_id=caiqcgroup.id group by ctrustinfo.cgroup_id order by ctrustinfo.cgroup_id' 
    inner_query =[]
    for x in range(len(cloud_list)):
        #print 'This is cloud#', cloud_list[x]              
        
        cloud_detail.append(profile_detail(cloud_list[x]))
        inner_query.append( 'max(case when ctrustinfo.cloud_id ='+cloud_list[x]+ ' then ctrustinfo.caiq_e end) as [Peer-'+str(x+1)+']') 
    
    #print 'This is inner query as list', inner_query
    
    inner_query = ",".join(inner_query)
    #print 'This is inner query as string ', inner_query
    full_query = query_start+inner_query+query_end  
   # print "This is the complete query", full_query  
    #print "this is cloud profile for all", cloud_detail 
    
    cur.execute(full_query)
    result_compare = cur.fetchall()
   # print result_compare
                  
    #full_query = query_start+query1+query_end
  #  print full_query
    
    return {'result_compare':result_compare, 'cloud_detail':cloud_detail}


def find_and(list_values):
    #print "this is find and "
     
    if list_values[2] =='Trust' and list_values[3]=="Trust":  
        return "Trust"
    else: 
        return "Distrust"
    
def find_or(list_values):
    if list_values[2] =='Trust' or list_values[3]=="Trust":  
        return "Trust"
    else: 
        return "distrust"
    
    


@webapp.route('/tpara',methods=['GET', 'POST'])
def trust_settings():
    return render_template("/tpara.html") 

@webapp.route("/graphy")   
def test_graph():
    DG=nx.DiGraph()
    DG.add_weighted_edges_from([(1,2,0.5), (3,1,0.75),(2,3,0.4),(1,4,0.6)])
    
   # print "This is node=",DG[1]
    print DG.nodes()
    print DG.edges()
    print "out degree=", DG.out_degree(1,weight='weight')
    print "Total degree=", DG.degree(1,weight='weight')
    print "Successor = ", DG.successors(1)
    print "Neighbors=", DG.neighbors(1)
    
    nx.draw(DG)
    plt.savefig("graph.png")
    
    
    return "Oki doki"


@webapp.route('/biddings', defaults={'sort': 'threshold', 'order': 'desc','filter':'requested'})
@webapp.route('/biddings/<string:sort>')
@webapp.route('/biddings/<string:sort>/<string:order>')
@webapp.route('/biddings/<string:sort>/<string:order>/<string:filter>')
def biddings(sort='id', order='asc', filter='requested'):
     
     
    query1= "select biddings.id,cloud_id,cprofile.cname,cprofile.cendpoint,timedate,(select statuscodes.description from statuscodes where statuscodes.code=biddings.status) as status,nopeers,threshold,type from biddings" 
    query2 = " INNER Join cprofile on biddings.cloud_id = cprofile.id "
    
    query3= " order by " + sort +' '+ order 
    #if not tfilter== 'all': 
    query4 = " where biddings.status= (select code from statuscodes where description='" + filter+ "')"
   
    print query1+query2+query4+query3
   
    cur.execute(query1+query2+query4+query3)
    all_biddings = cur.fetchall()
    tcount= len(all_biddings)
    
    return render_template('biddings.html', 
                           tcount=tcount, 
                           biddings=all_biddings, 
                           sort=sort, 
                           order=order,
                           filter=filter, 
                           page='biddings', 
                           is_xhr=request.is_xhr)


@webapp.route('/edit_transaction', methods=['GET', 'POST'] )
#@webapp.route('/edit/<string:cid>' )
def edit_transaction():
    
    if request.method == 'POST':
        
        if request.form['faction'] == 'New': 
            return redirect('/newtrans')
        
        if request.form['faction'] == 'join':
             
            return redirect("/newsubtrans")
    

    return redirect('/biddings')
 
#@webapp.route("/newtrans", methods=['GET', 'POST'])
@webapp.route("/newtrans/<string:cid>/<string:trx_type>", methods=['GET', 'POST'])
def newtrans(cid,trx_type):

    if request.method == 'GET': 
        cur.execute("select id,cname,cendpoint,avg_e from cprofile where id=?" , [(cid)]  )
        whois = cur.fetchone()
        if whois:
            print whois[0]
            print whois[1]
            print whois[2]
            print whois[3]
            status = 400
            curtime = datetime.now()
             
            
            
            cur.execute("INSERT INTO biddings(cloud_id,threshold,timedate,status,nopeers,type) VALUES (?,?,?,?,?,?)", (whois[0],whois[3],curtime,status,0,trx_type)  ) 
            conn.commit()
            message = "Started a new transaction with "+ str(whois[1])+ " as home cloud"
            flash(message)

    
    else: 
        flash("Error-420")
    return redirect('/biddings')
        
   # return render_template('newtrans.html', form=form,whois = whois , is_xhr=request.is_xhr )

