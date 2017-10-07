# coding=utf-8
import logging
import psutil
import socket
import uuid
import locale
import os
import openpyxl
import re
from psdash.node import LocalNode,RemoteNode
import sqlite3
import pygal
import networkx as nx
from networkx.drawing.nx_agraph import write_dot, graphviz_layout,to_agraph
import matplotlib.pyplot as plt  
import graphviz
import pydot 
from networkx import *
import pygraphviz as pgv

import datetime
from datetime import datetime, timedelta
import arrow as arw 


from openpyxl import load_workbook   
from openpyxl.compat import range
from openpyxl.cell import cell


from flask import Flask, render_template, request, session, abort
from flask import json, jsonify, Response, Blueprint, current_app, g, flash, redirect, url_for
from flask_wtf import FlaskForm
from wtforms import Form,TextField,TextAreaField,validators,StringField,SubmitField, FileField, IntegerField, PasswordField, BooleanField
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
from networkx.algorithms.traversal.breadth_first_search import bfs_edges
#from matplotlib.pyplot import arrow
from networkx.algorithms.shortest_paths.unweighted import predecessor
from networkx.algorithms.shortest_paths.generic import shortest_path_length
from wtforms.fields.simple import HiddenField
from wtforms.fields.core import SelectField, FloatField
from time import strftime


ALLOWED_EXTENSIONS = set(['txt', 'pdf', 'png', 'jpg', 'jpeg', 'gif', 'xlsx'])
ALLOWED_ANSWER_YES = set(['yes', 'Yes', 'YES', 'YEs','yES','yeS', 'Y', 'y', 'X', 'x'])
ALLOWED_ANSWER_NO = set(['no', 'No', 'NO', 'nO', 'N', 'n', 'X', 'x'])
ALLOWED_ANSWER_NA = set(['na', 'Na', 'NA', 'nA', 'N.A', 'N.A.', 'n.a.', 'N.a.','n.A.', 'not applicable','Not Applicable', 'not Applicable', 'Not applicable', 'X', 'x'])

logger = logging.getLogger('psdash.web')
webapp = Blueprint('psdash', __name__, static_folder='static')
conn = sqlite3.connect('db.sqlite3',detect_types=sqlite3.PARSE_DECLTYPES |sqlite3.PARSE_COLNAMES )

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
    if not session.get('logged_in'):
        return render_template('login.html')
    else:
        sysinfo = current_service.get_sysinfo()
        netifs = current_service.get_network_interfaces().values()
        netifs.sort(key=lambda x: x.get('bytes_sent'), reverse=True)
        print "Load average", sysinfo['load_avg']
     ##### resume here  
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

class SignupForm(Form):
       
 
    username = TextField('Username', validators=[validators.Length(min=4, max=20)])
    password = PasswordField('New Password', validators=[
        validators.Required(),
        validators.EqualTo('confirm', message='Passwords must match')
    ])
    confirm = PasswordField('Repeat Password')
    accept_tos = BooleanField('I accept the Terms of Service and Privacy Notice (updated Aug 22, 2017)', validators=[validators.Required()])

class RegisterForm(Form):
    
    login_id = HiddenField('Login ID:', validators=[validators.required()])
    username = TextField('User Name:', validators=[validators.required()])
    cname = TextField('Cloud Name:', validators=[validators.required()])
    cmeta = TextField('Metatext:', validators=[validators.required()])    
    cendpoint = TextField('Endpoint URL:', validators=[validators.required()])
    cinfo = TextField('Description:', validators=[validators.required()])
        
class AddResource(Form):
     
    transaction_id = TextField('Transaction ID')
    hcloud_id = TextField('HCloud ID')
    hcloud_name = TextField('Home Cloud')
    cloud_id = HiddenField('Cloud ID',[validators.Required()])
    cname = TextField('Cloud Name:',[validators.Required()])
    
    rtype = SelectField(u'Resource Type', choices=[('1','IaaS')])
    #resource = SelectField(u'Resource', choices=[('1','Compute'),('2','Storage'),('3','Network')])
    resource = SelectField(u'Resource', coerce=int)
    #rthreshold = FloatField('Trust threshold', validators = [validators.Required() ])
    rvalue = FloatField('Resource Value', validators = [validators.Required() ])
    uom =  TextField('Unit', [validators.Required()])
    rqty = IntegerField('Quantity', [validators.Required()] )
    lstart =  TextField('Time to start', [validators.Required()])
    expiresat =  TextField('Expires at :', [validators.Required()])
    accept_tos = BooleanField('I accept the Terms of Service and Privacy Notice (updated Sept 22, 2017)', [validators.Required()])
    
    def set_choices(self):
        cur.execute("select id, rname from resources")
        resource_choices= cur.fetchall()
        print resource_choices
        self.resource.choices = resource_choices 

    
@webapp.route("/signup", methods=['GET', 'POST'])
def signup():
    form = SignupForm(request.form)
    print form.errors
    
    if request.method == 'POST':
        
        username = slugify(request.form['username'],1) # convert to lower here -- 
        password = request.form['password']
        confirm = request.form['confirm']
        
        if form.validate():
            print username
            cur.execute('select * from login where username=?', [(username)])
            rows = cur.fetchall()
            if rows: 
                flash("That username is already taken, please choose another")
                return render_template('signup.html', form=form, is_xhr=request.is_xhr)
            else:
                cur.execute('INSERT Into login(username,password,access_level) values (?,?,?)', (username, password, 4))
                conn.commit()
                cur.execute('select * from login where username = :1 ', [(username)])
                whois = cur.fetchone()
                print "This is user id ", whois[0]

                if whois:
                    print whois[0]
                    print whois[1]
                    session['login_id'] = whois[0]
                    session['username'] = whois[1]
                    session['access_level'] = whois[2]    
                
#                return redirect('/upload') # this will be register a cloud. 
                    #return render_template('cregister.html')
                return redirect("/cregister")
        else:
            flash('All the form fields are required. ')
 
    return render_template('signup.html', form=form,is_xhr=request.is_xhr )

@webapp.route("/cregister", methods=['GET', 'POST'])
def register():
    
    print "gotcha0"
    form = RegisterForm(request.form) # create the registration form 
    #print form.errors
    print "gotcha1"
    
    if request.method == 'POST': # receive the value if request is posted 
        
        login_id = request.form['login_id']
        username = request.form['username']
        cname= request.form['cname']
        cmeta = request.form['cmeta']
        cendpoint = request.form['cendpoint']
        cinfo = request.form['cinfo']
        print "gotcha2"
        if form.validate():
            print "gotcha3"
            print login_id 
            cur.execute('select id,cname from cprofile where login_id=?', [(login_id)])
            row = cur.fetchone() # if there is any other cloud with this login iD 
            if row:
                print "inside cur.rowcount" 
                flash("A cloud provider is already registered with this Login ID.")
                print "A cloud provider is already registered with this Login ID."
                print row[0]
                cur.execute("select * from caiqanswer where cloud_id=:1 ", [(row[0])])
                rows = cur.fetchall()
                if len(rows) < 1: 

                    flash("Complete the signup process by uploading your caiq")
                    session['cloud_id'] = row[0]
                    session['cloud_name'] = row[1]
                    
                    return render_template('upload.html')
                      
                else: # register another cloud 
                    print "Register another cloud with another name "
                    return render_template('signup.html', form=form, is_xhr=request.is_xhr)
                    
            else:
                cur.execute("INSERT INTO cprofile(cname,cmeta,cendpoint,cinfo, login_id) VALUES (?,?,?,?,?)", (cname,cmeta,cendpoint,cinfo, login_id)  ) 
                conn.commit()
                flash('New cloud added to database successfully.') #Display a message to end user at front end.
                print 'New cloud added to database successfully.' #Display a message to end user at front end.
                cur.execute("select id, cname from cprofile where cname=?" , [(cname)]  )
                whois = cur.fetchone()

                if whois:                   
                    session['cloud_id'] = whois[0]
                    session['cloud_name'] = whois[1]
                    
                    return redirect('/upload') # this will be register a cloud. 
        else:
            flash('All the form fields are required. ')
 
    return render_template('cregister.html', form=form,is_xhr=request.is_xhr )


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
            svar_cname = session.get('cloud_name', None)
            svar_cid = session.get('cloud_id', None)
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
            
            print "This is where to look Login ID=", session['login_id']
            cur.execute('update login set access_level=2 where login.id=?', [(session['login_id'])])
            conn.commit()
                
            return render_template("login.html")
    return render_template('upload.html')



def check_login(): 
    if not session.get('logged_in')==True:
        print "i am alive"
        return redirect('/login')
        print "i am alive2"
    else:
        print "i am alive3"
        
        return redirect( request.referrer)
@webapp.route("/logout")
def logout():
    session['logged_in'] = False
    #session.clear()
    
    return redirect('/login')
        
@webapp.route("/login", methods=['GET','POST'])
def web_login():
    
    if request.method == 'POST': 
        username = request.form['username']
        password = request.form['password']
    
        cur.execute('select * from login where username = lower(:1) and password=:2', (username, password))
        whois = cur.fetchone()
        print whois
        if whois: 
            session['logged_in'] = True
            session['login_id'] = whois[0]
            session['username'] = whois[1]
            
            session['access_level'] = whois[3]
            if whois[3] == 4: 
                
                return redirect("/cregister")
            else:
                reg_clouds = []
                cur.execute('select id,cname from cprofile where login_id =?', [(session['login_id'])])
                cloud_ids =  cur.fetchall()
                for clouds in cloud_ids: 
                    reg_clouds.append(clouds)
                session['reg_clouds'] = reg_clouds
                print session['reg_clouds']
                
                return redirect("/profiles")
        else:
            flash('wrong password!')
            return render_template('login.html') 
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
    
    #check_login()
    query1= "select id,cname,cendpoint,strftime('%s','now','localtime') - strftime('%s',lastseen) AS 'timesince',strftime('%Y-%m-%d', lastseen),avg_e, strftime('%H:%M:%S', lastseen) from cprofile  order by " 
    query2 =  sort + ' ' + order
    #print query1+query2
    cur.execute (query1+query2)
    profiles = cur.fetchall()

    return render_template('profiles.html', profiles=profiles, sort=sort, order=order, page='profiles', is_xhr=request.is_xhr)

@webapp.route('/profile/<int:cid>', defaults={'section': 'overview'})
@webapp.route('/profile/<string:cid>', defaults={'section': 'overview'})
@webapp.route('/profile/<int:cid>/<string:section>')
def profile(cid, section):
    #print "This is profile function", cid
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
        
        if request.form['faction'] == 'Refresh Cart':
            if 'cart' not in session:
                flash ("Nothing to do")
                return redirect("/profiles")
            else: 
                cloud_list= []
                for cid in session.pop('cart', []):
                    cloud_list.append(cid)
                flash ("Cart is refreshed")
                return redirect("/profiles")
            
        if request.form['faction'] == 'compare':  
            if 'cart' not in session:
                flash ("Nothing to compare")
                return redirect("/profiles")
            else: 
                return redirect("/compare")
            
        if request.form['cid'] and request.form['faction'] == 'delete':
            cid = request.form['cid']
            faction = request.form['faction']
            
            print 'For delete=', faction, cid
                     
            cur.execute( 'delete from cprofile where id IN (:1)',([cid]))
            conn.commit()
            #msg='Profile deleted successfully'
            flash('Profile deleted successfully')
            print("affected rows = {}".format(cur.rowcount))
                 
    return redirect('/profiles')

  
@webapp.route('/compare', defaults={'section': 'overview'})
@webapp.route('/compare/<string:section>')
def compare_trust(section):
    #print "This is profile function", cid
    valid_sections = [
        'overview',
        'objective',
        'subjective',
        'graph',
        ]

    if section not in valid_sections:
        errmsg = 'Invalid subsection when trying to view detail' 
        return render_template('error.html', error=errmsg), 404
    
    #cloud_list=[]
    
    print "session cart ", session['cart']
     
#     for cid in session.pop('cart', []):
#         cloud_list.append(cid)
    cloud_list = (session['cart'])
    print cloud_list
    print len(cloud_list)
    if len(cloud_list)==1: 
        flash("Can't compare single item") 
        return redirect("/profiles") 
    
    fetch_data = caiq_compare(cloud_list)
    result_compare = fetch_data.get('result_compare')
    cloud_detail = fetch_data.get('cloud_detail')
    parsed_result = fetch_data.get('parsed_result')
    graph_data = draw_graph1(cloud_list)
    
    context = {
        'parsed_result': parsed_result,
        'cloud_detail': cloud_detail,
        'cloud_list':cloud_list,
        'result_compare':result_compare,
        'graph_data': graph_data,
        'section': section,
        'page': 'compare',
        'is_xhr': request.is_xhr
    }
    
    if section == 'objective':
        context['objective'] = 'This is objective trust comparison' 
    elif section == 'subjective':
        context['subjective'] = 'This is subjective trust comparison'
    elif section == 'graph':
        context['graph'] = 'This is graphical representation of trust'
 
    return render_template(
        'compare/%s.html' % section,
        **context
    )

@webapp.route('/add_to_compare/<string:cid>' )
def add_to_compare(cid):
    if 'cart' in session:
        if len(session['cart']) == 4:
            flash('Cart Full..Cant add more')
            exit
        else:
            session['cart'].append(cid)
            flash(len(session['cart']))
    else:
        session['cart'] = [cid]
        flash(len(session['cart']))
    return redirect( request.referrer)    

def draw_graph(cid): # dont delete now..delete when  it will definitly merge into the new function 
    
    #Plotting all values for trust as graph    
    print "This is graph func ", cid
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
    return graph_data


def draw_graph1(cloud_list):
        
    print "This is the new graph func ", cloud_list
    
    graph = pygal.Line(range=(0,1.2 )) 
    graph.title = 'Comparison of Trust Evaluation for all selected clouds '
    x_label = []
     
    cur.execute('select short_code from caiqcgroup ORDER BY id')
    groups = cur.fetchall()
    for group in groups: # graph labels 
        x_label.append(group[0])    
    graph.x_labels = x_label
 
    
    for cid in cloud_list:
        cur.execute('select cname from cprofile where id=:1',[(cid)])
        cloud_name = cur.fetchone()
        
         
        full_query= '''select ctrustinfo.caiq_e,ctrustinfo.cgroup_id,
                    caiqcgroup.group_name,caiqcgroup.short_code 
                    from ctrustinfo inner join caiqcgroup on ctrustinfo.cgroup_id=caiqcgroup.id 
                    and ctrustinfo.cloud_id= (%s) ORDER BY cgroup_id''' % cid 
 
        mark_list =[]
        cur.execute(full_query)
        c_trust_eval = cur.fetchall()
        
        for x in c_trust_eval:
            mark_list.append(x[0])
        graph.add('(%s)' % cloud_name  , mark_list)

    graph_data = graph.render_data_uri()    # drawing the graph
    
    return graph_data


def caiq_trust_score(cid): # its one time function for every profile. never call twice 
    
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

#@webapp.route('/composite/<string:cloud_list>')
#def trust_table(cloud_list): # result compare, cloud_detail 
 
def sub_comp_trust(child,pred):   
 # Note: trust values must scale in the range (distrust,uncertainity,trust) or 
    # between (strong distrust, weak distrust, uncertainity, weak trust, strong trust)
    parsed_result = []
    cloud_list = pred, child
    print cloud_list
    result_compare = caiq_compare(cloud_list).get('result_compare')
    cloud_detail = caiq_compare(cloud_list).get('cloud_detail')
    result_tcp = []
    result_tparent = []
    
    for x in range(len(result_compare)):
        temp1=[]
        for y in range(len(result_compare[x])): 
            if y==0 or y==1:
                temp1.append(result_compare[x][y])
                continue
            temp1.append(parse_trust(result_compare[x][y]))
#             if y==2: 
#                 result_a.append(parse_trust(result_compare[x][y]))       
        
        parsed_result.append(temp1)
        result_tcp.append(tchild_parent(temp1).get('trust_code'))
        
    print "result tcp", result_tcp.count(1)
    print "result tcp", result_tcp.count(2)
    print "result tcp", result_tcp.count(3)
   # pr_trust_b_g_a = pr_trust_bga(result_tcp)
    #pr_trust_a = pr_trust_b_g_a(result_a)
    
    print "This is trust for child_given_parent", result_tcp
    print "This is trust for parent", result_tparent
    
    #print "This is parsed result", parsed_result
    
    return {'parsed_result':parsed_result,'cloud_detail':cloud_detail, 
            #'pr_trust_b_g_a':pr_trust_b_g_a
            }      

def parse_trust(caiq_score):
    # this is the point where a CSP will define its level of trust (May be we will see where the cloud score is itself)  

    if caiq_score>=0 and caiq_score <= 0.4: caiq_score= 'Distrust' 
    elif caiq_score > 0.4 and caiq_score<=0.5: caiq_score='Uncertain'
    elif caiq_score > 0.5 and caiq_score<=1 : caiq_score='Trust'
    
    else: 
        caiq_score="Fake assessment"
    
    sub_trust_value = caiq_score 
    return sub_trust_value

def caiq_compare(cloud_list):
    
    parsed_result = []
    cloud_detail = []
    query_start= 'select ctrustinfo.cgroup_id, caiqcgroup.group_name,' 
    query_end= 'from ctrustinfo INNER JOIN caiqcgroup on ctrustinfo.cgroup_id=caiqcgroup.id group by ctrustinfo.cgroup_id order by ctrustinfo.cgroup_id' 
    inner_query =[]
    #print "This is cloud list ", cloud_list
    for x in range(len(cloud_list)):
        cloud_detail.append(profile_detail(cloud_list[x]))
        inner_query.append( 'max(case when ctrustinfo.cloud_id ='+str(cloud_list[x])+ ' then ctrustinfo.caiq_e end) as [Peer-'+str(x+1)+']') 
        
    inner_query = ",".join(inner_query)
    full_query = query_start+inner_query+query_end  
    cur.execute(full_query)
    result_compare = cur.fetchall()


    for x in range(len(result_compare)):
        temp3=[]
        for y in range(len(result_compare[x])): 
            if y==0 or y==1:
                temp3.append(result_compare[x][y])
                continue
            temp3.append(parse_trust(result_compare[x][y]))

        parsed_result.append(temp3) 
    print parsed_result
       
    return {'result_compare':result_compare, 'cloud_detail':cloud_detail, 'parsed_result':parsed_result}

def tchild_parent(list_values):
    
    
    if list_values[2] =='Trust' and list_values[3]=="Trust":  
        return {'trust_code':1} # trust of child given trust of parent 
    elif list_values[2] =='Distrust' and list_values[3]=="Trust":
        return {'trust_code':2} # trust of child given distrust of parent
    elif list_values[2] =='Uncertain' and list_values[3]=="Trust":
        return {'trust_code':3} # trust of child given uncertainity of parent
    else: 
        return {'trust_code':4} # error
    
    
def pr_trust_bga(tcounter):
    
    count_trust = tcounter.count("Trust")
    print count_trust
    print float(count_trust)/16 # equal to P(tA|tS) 
    count_distrust = tcounter.count("Distrust") # equal to P(tA|dS) 
    print float(count_distrust)/16  
    
    count_uncertain = tcounter.count("Uncertain") # equal to P(tA|dS) 
    print float(count_uncertain)/16  
    
    return {'count_trust': count_trust, 'count_distrust': count_distrust, 'count_uncertain': count_uncertain}
    
    
def find_or(list_values):
    if list_values[2] =='Trust' or list_values[3]=="Trust":  
        return "Trust"
    else: 
        return "distrust"

@webapp.route('/tpara',methods=['GET', 'POST'])
def trust_settings():
    return render_template("/tpara.html") 

#@webapp.route("/tdgraph")  # trust dependency as graph:: comment for live   
#def td_graph():
def td_graph(nlist,elist):  
    DG=nx.DiGraph()
    
    print nlist
    print elist
      
#     nlist = [('S',{'e-score':0.9}) , ('A',{'e-score':0.92}), ('B',{'e-score':0.91}) , 
#              ('C',{'e-score':0.87}), ('D',{'e-score':0.79}) , ('F',{'e-score':0.90}), 
#              ('I',{'e-score':0.75}) , ('L',{'e-score':0.8}), ('M',{'e-score':0.85}) , 
#              ('Q',{'e-score':0.9})          
#             ]   # This list contains all participants of federation 
# #                 #can pass this node list with weights from the web frontend 

    DG.add_nodes_from(nlist)
    
    #This is a transaction 
    #elist = [('S','A'),('S','B'),('S','C'), ('A','D'), ('B','F'), ('C','I'),('D','L'), ('F','L'), ('F','M'), ('I','L'), ('L','Q'), ('M','Q')]
#     elist = [('S','A'),('S','B'),('S','C'), ('A','D'), ('B','F'), ('C','I'),('D','L'), ('F','L'), ('F','M'), ('I','L')]
#     nlist = nlist 
#     elist = elist
#     
    blist=[] # will contain transformed alist as a weighted edge list  
    
    for myedges in elist: 
        #calculate edge weight as multiple of its node weights 
        print "my edges",myedges[0], myedges[1] 
       # print DG.node[myedges[0]]['e-score']
        edge_weight = round((DG.node[myedges[0]]['e-score'] * DG.node[myedges[1]]['e-score']),4)
        my_weighted_edge = (myedges[0], myedges[1], edge_weight)
        blist.append(my_weighted_edge) #  
        print "This is blist",blist 
                 
    DG.add_weighted_edges_from(blist) 
    caiq_obj_trust_values = caiq_obj_trust(DG)
    print caiq_obj_trust_values
    
#     nx.draw_networkx(DG)
#          
#     plt.savefig("static/graph.png")
#     plt.clf() 
#       
    return {"obj_trust":caiq_obj_trust_values, "graph_data":DG}  
    
def caiq_obj_trust(graph):
    
    DG = graph
    ####
    ####  NOW JUST SUM THE EDGES IN A PATH 
    ####
    ####
    
    # get the root node a.k.a home cloud 
    root_nodes= [node for node in DG.nodes() if DG.in_degree(node)==0 and DG.out_degree(node)!=0]
    leaf_nodes = [node for node in DG.nodes() if DG.in_degree(node)!=0 and DG.out_degree(node)==0]
    print "root Node is ", root_nodes
    print "leaf nodes are ", leaf_nodes
    
    graph_weight = [] # global trust of this transaction 
    count_paths = []
    tfactor = []
    # traversing all possible paths between a root and its leaves 
    for leaf_node in leaf_nodes:
        #root_nodes= [node for node in DG.nodes() if DG.in_degree(node)==0 and DG.out_degree(node)!=0]
        print root_nodes,'-->' , leaf_node
        for root_node in root_nodes:
            sub_graph_weight = 0 # weight for each sub graph having same root same leaf node  
            count_sub_paths = 0  
            #for p in nx.all_shortest_paths(DG,source=root_node,target=leaf_node):
            for p in nx.all_simple_paths(DG,source=root_node,target=leaf_node):     
                print "I am P", p
                path_weight = 0 # weight for each path 
                for x in range(len(p)-1):
                    path_weight += DG.get_edge_data(p[x],p[x+1])['weight']
                    print p[x],'-->',p[x+1], path_weight
                avg_path_weight= path_weight/(len(p)-1)
                print p, '-->', avg_path_weight
                sub_graph_weight += path_weight/(len(p)-1)
                count_sub_paths+=1
            avg_subg_weight = sub_graph_weight/count_sub_paths
            print "Total paths in this sub graph ", count_sub_paths
            print "weight for this sub-graph is ", sub_graph_weight
            print "Average subgraph weight ", avg_subg_weight
        count_paths.append(count_sub_paths)
        print "Total paths = ", count_paths
        graph_weight.append(avg_subg_weight)
    print graph_weight
    print count_paths
    
    
    ####
    ### Important note: when the graph has more than one leaves add weight to each path according to the 
    ## ratio of its paths to total paths 
    ## e.g. A graph has two leaves with  two paths - its the average 
    ##    when it has two leaves with one and three paths - make it different !! Think 
    ## number of sub paths div by total paths is the factor of that path 
    ### LOGIC: More the number of paths less reliable OR more the number of nodes in a path less reliable
    ####                
    
    final_trust=0
    for x in range(len(count_paths)): 
        print float(count_paths[x])/sum(count_paths) # total paths in a subgraph / total paths in graph 
        tfactor.append(float(count_paths[x])/sum(count_paths))
        final_trust+= (float(count_paths[x])/sum(count_paths))*graph_weight[x]
        
    print tfactor,final_trust
    # factorizing ends here 
    
    return {'tfactor':tfactor, 'final_trust':final_trust}

@webapp.route('/biddings', defaults={'trid':'0', 'sort': 'threshold', 'order': 'desc','filter':'RTL'})
@webapp.route('/biddings/<string:trid>')
@webapp.route('/biddings/<string:trid>/<string:sort>')
@webapp.route('/biddings/<string:trid>/<string:sort>/<string:order>')
@webapp.route('/biddings/<string:trid>/<string:sort>/<string:order>/<string:filter>')
def biddings(trid='0', sort='biddings.id', order='asc', filter='RTL'):
    
# select biddings.id,biddings.resource_id, 
# (select resources.rname from resources where resources.id=biddings.resource_id) as rname,
# (select resources.uom from resources where resources.id=biddings.resource_id) as uom ,
# (select resources.rtype from resources where resources.id=biddings.resource_id) as rtype,
# cloud_id,cprofile.cname,cprofile.cendpoint,postedat,expiresat,
# (select statuscodes.description from statuscodes where statuscodes.code=biddings.status) 
# as status,threshold,bidtype from biddings 
# INNER Join cprofile on biddings.cloud_id = cprofile.id  where biddings.bidtype=('RTA') 
# order by resource_id desc
    print trid
    
    query1= '''select biddings.id,cloud_id,cprofile.cname,cprofile.cendpoint,
                (select cprofile.cname 
                from transactions inner join cprofile on transactions.hcloud_id=cprofile.id 
                and transactions.id=transaction_id),transaction_id,
                (select foreignpeers from transactions where transactions.id=transaction_id), 
                biddings.resource_id,(select resources.rname from resources 
                where resources.id=biddings.resource_id) as rname,
                biddings.value, (select resources.uom from resources where resources.id=biddings.resource_id) 
                as uom ,
                (select rcategory.cat_name from rcategory INNER join resources on 
                rcategory.id = resources.rtype_id where  resources.id=biddings.resource_id) as rtype,
                strftime('%H:%M:%S',postedat),strftime('%d-%m-%Y',postedat),
                strftime('%H:%M:%S',startsat),strftime('%d-%m-%Y',startsat), 
                strftime('%H:%M:%S',expiresat),strftime('%d-%m-%Y',expiresat),'''
#     strftime('%H:%M:%S',startsat),strftime('%d-%m-%Y',startsat),
#     strftime('%H:%M:%S',expiresat),strftime('%d-%m-%Y',expiresat),''' This is some bug :will fix soon
    
    query2= "(select statuscodes.description from statuscodes where statuscodes.code=biddings.status) as status,threshold,bidtype from biddings" 
    query3 = " INNER Join cprofile on biddings.cloud_id = cprofile.id "
     
    query5= " order by " + sort +' '+ order 
    #if not tfilter== 'all': 
    query4 = " where biddings.bidtype=('" + filter+ "')" 
    
    final_query = query1+query2+query3+query4+query5
   
    print final_query   
    cur.execute(final_query)
    all_biddings = cur.fetchall()
    tcount= len(all_biddings)
    
    return render_template('biddings.html', 
                           tcount=tcount, 
                           biddings=all_biddings, 
                           sort=sort, 
                           order=order,
                           filter=filter, 
                           page='biddings',
                           trid = trid,
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
# @webapp.route("/newtrans/<string:cid>/<string:trx_type>", methods=['GET', 'POST'])
# def newtrans(cid,trx_type):
# 
#     if request.method == 'GET': 
#         cur.execute("select id,cname,cendpoint,avg_e from cprofile where id=?" , [(cid)]  )
#         whois = cur.fetchone()
#         if whois:
#             print whois[0]
#             print whois[1]
#             print whois[2]
#             print whois[3]
#             status = 400
#             curtime = datetime.utcnow()
# 
#             cur.execute("INSERT INTO biddings(cloud_id,threshold,timedate,status,nopeers,type) VALUES (?,?,?,?,?,?)", (whois[0],whois[3],curtime,status,0,trx_type)  ) 
#             conn.commit()
#             message = "Started a new transaction with "+ str(whois[1])+ " as home cloud"
#             flash(message)
# 
#     
#     else: 
#         flash("Error-420")
#     return redirect('/biddings')
        
   # return render_template('newtrans.html', form=form,whois = whois , is_xhr=request.is_xhr )

@webapp.route('/edit_bidding', methods=['GET', 'POST'] )
def edit_bidding():
    
    if request.method == 'POST':
        
        if request.form['bidaction'] == 'Composite':
            flash ("Nothing to do")
            return redirect("/biddings")
        
        if request.form['bidaction'] == 'Add free resource':
            flash ("Supply a resource")
            return redirect('/addrtl')     
        if request.form['bidaction'] == 'Request for resource' :
            flash ("Demand a resource")
            return redirect('/addwta')
#         if request.form['bidaction'] == 'Engage':
#             
#             flash ("Starting a new transaction")
#             hcloud_id = request.form['hcloud_id']
#             fcloud_id = request.form['fcloud_id']
#             print add_transaction(hcloud_id,fcloud_id)
#             
#             # This will redirect to show transaction page which is not available now   
#             # Till then it is redirecting to biddings page  
#             #
#             return redirect('/biddings')
        
    return "Hello This is edit bidding"


def add_demand():
    print "This is demand"
    return "This is demand" 

@webapp.route("/addrtl",methods=['GET', 'POST'])
def addrtl():
    #n = RemoteNode('Alertlogic', '192.168.10.20', 5000)
    #print 'This is n', n.get_id()
    #current_s = n.get_service()
    #print current_s.get_disks()
    
    form = AddResource(request.form)
#     cur.execute("select id, rname from resources")
#     resource_choices= cur.fetchall()
#     print resource_choices
    
    form.set_choices()
    #storage =  round ( current_service.get_disks()[0]['space_free']/float((1000*1000*1000)),2)

    #storage =  round ( current_s.get_disks()[0]['space_free']/float((1000*1000*1000)),2)
    if request.method == 'POST':
        
        cloud_id = request.form['cloud_id']
        cname = request.form['cname']
        rtype = request.form['rtype']
        resource = request.form['resource']
        rvalue = request.form['rvalue']
       # rthreshold = request.form['rthreshold']
        uom =  request.form['uom']
        rqty = request.form['rqty']
        lstart =  request.form['lstart']
        expiresat =  request.form['expiresat']
        accept_tos = request.form['accept_tos']
        cur_time = datetime.now()
        
        if form.validate():
            
            cur.execute( 'insert into biddings(bidtype,cloud_id,postedat,startsat,expiresat,status,threshold,resource_id,value,quantity) values (?,?,?,?,?,?,?,?,?,?)', ('RTL',cloud_id,cur_time,lstart,expiresat,600,0.90,1,rvalue,rqty))  
            conn.commit()
            return redirect( request.referrer)
        else:
            
            flash('All the form fields are required. ')
        
    return render_template('addresource.html', form=form,is_xhr=request.is_xhr )
    
    
@webapp.route('/addwta', defaults={'trid': 0}, methods=['GET', 'POST'])
@webapp.route("/addwta/<string:trid>", methods=['GET', 'POST'])
def addwta(trid):
    form = AddResource(request.form)
    form.set_choices()
#     cur.execute("select id, rname from resources")
#     resource_choices= cur.fetchall()
#     print resource_choices
    
#     if request.method == 'GET':
    print "This is tr id ", trid
    cur.execute("select id,(select cname from cprofile where cprofile.id = transactions.hcloud_id), hcloud_id from transactions where id=?", [(trid)])
    transaction_data= cur.fetchone()
    print transaction_data
        
    if request.method == 'POST':  
        transaction_id = request.form['transaction_id']
        hcloud_id = request.form['hcloud_id']
        hcloud_name = request.form['hcloud_name']
        cloud_id = request.form['cloud_id']
        cname = request.form['cname']
        rtype = request.form['rtype']
        resource = request.form['resource']
        rvalue = request.form['rvalue']
        uom =  request.form['uom']
        rqty = request.form['rqty']
        lstart =  request.form['lstart']
        expiresat =   request.form['expiresat']
        accept_tos = request.form['accept_tos']
        cur_time = datetime.now()
        
        if form.validate():
            cur.execute( 'insert into biddings(bidtype,cloud_id,transaction_id,postedat,startsat,expiresat,status,threshold,resource_id,value,quantity) values (?,?,?,?,?,?,?,?,?,?,?)', ('WTA',cloud_id,transaction_id,cur_time,lstart,expiresat,600,0.90,1,rvalue,rqty))  
            conn.commit()
        
            return redirect( "/biddings")
        else:
            
            flash('All the form fields are required. ')
        
    #return render_template('rfr.html', transaction_data=transaction_data,form=form,is_xhr=request.is_xhr )
    return render_template('search.html', transaction_data=transaction_data,form=form,is_xhr=request.is_xhr )
    
#@webapp.route("/dep_graph/<int:cid>")
@webapp.route("/dep_graph/<int:cid>/<string:trid>")
@webapp.route("/dep_graph/<int:cid>/<string:trid>/<string:engage>")

def dep_graph(cid,trid=0, engage='False'): 
    print "this is make graph function "
    print trid
    print engage
    
    nlist = [] # contains the final output 
    elist = []
    
    fcloud_id = cid
    foreign_cloud = 0
    fcloud_e = 0
    
    hcloud_id = 0
    hcloud_e=0
    home_cloud=0
    
    parent_cloud_id = 0
    parent_cloud_e = 0
    parent_cloud = 0
    cur_time = datetime.now()
# 
# This format is reequired as json 
# [('S',{'e-score':0.9}) , ('A',{'e-score':0.92})
#
    if trid=='0':
        cur.execute("select id,cname,avg_e from cprofile where login_id=:1", [(session['login_id'])] )
        parent_cloud = home_cloud = cur.fetchone()
        parent_cloud_id = hcloud_id = home_cloud[0]
        parent_cloud_e = hcloud_e = home_cloud[2]
        
        nlist.append((hcloud_id,{ "e-score":hcloud_e})) 
        
        cur.execute("select id,cname,avg_e from cprofile where id=:1", [(fcloud_id)] )
        foreign_cloud = cur.fetchone()
        fcloud_id = cid 
        fcloud_e = foreign_cloud[2]
        nlist.append((fcloud_id,{ "e-score":fcloud_e}))
        
        myedge = (hcloud_id,fcloud_id)
        elist.append(myedge)
#         print "Final output =", nlist
#         print "Final edges = ", elist 
#         
#         print "my edges ", myedge
#         print "Elist", elist
#         ## save this list in db 
        
        td_graph_data = td_graph(nlist, elist)
        obj_trust = td_graph_data.get('obj_trust')
        DG = td_graph_data.get('graph_data')
#         print obj_trust
        
        if engage == 'True':
            print "Starting a new transaction "
            print "this is add_transaction adding ", hcloud_id, "  +  ", fcloud_id, obj_trust['final_trust']
            
#             cur.execute("""insert into transactions(hcloud_id,lastpeer, foreignpeers,comptrust,status,creationtime,
#                     lastactivity,tthreshold,nlist,elist) values(?,?,?,?,?,?,?,?,?,?)""", 
#                     (hcloud_id,fcloud_id,1, obj_trust['final_trust'],710, datetime.now(),datetime.now(),0.75,str(nlist),str(elist) ))

            cur.execute("""insert into transactions(hcloud_id,lastpeer, foreignpeers,comptrust,status,creationtime,
                    lastactivity,tthreshold) values(?,?,?,?,?,?,?,?)""", 
                    (hcloud_id,fcloud_id,1, obj_trust['final_trust'],710, cur_time,cur_time,0.75))
            conn.commit()
            cur.execute("select MAX(ID) from transactions")
            trid = cur.fetchone()
            print trid[0]
            
#             cur.execute("""insert into subtrans (transaction_id, cloud_id,parent_id,resource_id,timestamp,status)
#              values (?,?,?,?,?,?) """, trid[0],hcloud_id,parent_cloud_id,1,datetime.now(),710)
#             conn.commit() # This was the home cloud as parent cloud cloud also 
            
#             query_check =("""insert into subtrans (transaction_id, cloud_id,parent_id,resource_id,timestamp,status)
#             values (?,?,?,?,?,?) """, trid[0],fcloud_id,parent_cloud_id,1,datetime.now(),710)
#             
#             print query_check
#    
            print cur_time,fcloud_id,parent_cloud_id,710
            cur.execute("""insert into subtrans (transaction_id, cloud_id,parent_id,resource_id,timestamp,status)
            values (?,?,?,?,?,?) """, (trid[0],fcloud_id,parent_cloud_id,1,cur_time,710))
            conn.commit()
             
          
            json_fnlist = 'static/trx-json/'+str(trid[0])+'-'+str(hcloud_id)+'-nlist.json'
            json_felist = 'static/trx-json/'+str(trid[0])+'-'+str(hcloud_id)+'-elist.json'
            
            print json_fnlist
            print json_felist
            
            with open(str(json_fnlist), 'w') as fp:
                json.dump(nlist, fp)
            
            with open(str(json_felist), 'w') as fp:
                json.dump(elist, fp)
            
            flash ("Started a new transaction ")
            return redirect("/transactions")
            
        #make_new_graph()
    elif trid!='0':
        #append_graph()
        print "This is old trx"
        cur.execute("select * from transactions where id=?", [(trid)])
        transaction_detail = cur.fetchone()
        hcloud_id = transaction_detail[1]
        
        cur.execute("select id, cname,avg_e from cprofile where id=?", [(hcloud_id)])
        home_cloud = cur.fetchone()
        hcloud_id = home_cloud[0]
        hcloud_e = home_cloud[2]
        
        foreign_peers = transaction_detail[2]
        print "Foreign Peers", foreign_peers
        
        cur.execute("select id,cname,avg_e from cprofile where login_id=:1", [(session['login_id'])] )
        parent_cloud = cur.fetchone()
        parent_cloud_id = parent_cloud[0]
        parent_cloud_e = parent_cloud[2]
        
        #parent_cloud_id = session['reg_clouds'][0][0]
        
        fcloud_id = cid
        print "Joining an old transaction with home cloud", hcloud_id, "I am ", parent_cloud_id, "demanding a resources from ", fcloud_id
        
        json_fnlist = 'static/trx-json/'+str(trid)+'-'+str(hcloud_id)+'-nlist.json'
        json_felist = 'static/trx-json/'+str(trid)+'-'+str(hcloud_id)+'-elist.json'
        
        with open(str(json_fnlist), 'r') as fp:
            ndata = json.load(fp)
        
        with open(str(json_felist), 'r') as fp:
            edata = json.load(fp)
        #print jdata
        # json.dumps(jdata) # this converts everything to string/characters
        nlist = list(ndata)
        elist = list(edata)

#         print nlist
#         print nlist[0][1]['e-score']
        
        cur.execute("select id,cname,avg_e from cprofile where id=:1", [(fcloud_id)] )
        foreign_cloud = cur.fetchone()
        fcloud_e = foreign_cloud[2]
        nlist.append([fcloud_id,{ u'e-score':fcloud_e}])
        
#         with open(str(json_fnlist), 'w') as fp:
#             json.dump(nlist, fp)
        
        #myedge = (parent_cloud_id,fcloud_id)
        elist.append([parent_cloud_id,fcloud_id])
        
#         print elist, 'elist NOW'
#         print "parsing elist ", elist[0][0], elist[0][1]
#         print nlist
        
#         with open(str(json_felist), 'w') as fp:
#             json.dump(elist, fp)
                
        td_graph_data = td_graph(nlist, elist)
        obj_trust = td_graph_data.get('obj_trust')
        DG = td_graph_data.get('graph_data')
        
        if engage == 'True': 
            print "Updating an old transaction "
            with open(str(json_fnlist), 'w') as fp:
                json.dump(nlist, fp)
            with open(str(json_felist), 'w') as fp:
                json.dump(elist, fp)   
            cur_time = datetime.now()
            foreign_peers = foreign_peers+1
            cur.execute("update transactions set foreignpeers=:1, comptrust=:2,lastpeer=:3,lastactivity=:4 where id=:5 ", (foreign_peers, obj_trust['final_trust'],fcloud_id,cur_time,trid))
            conn.commit()
            
            cur.execute("""insert into subtrans (transaction_id, cloud_id,parent_id,resource_id,timestamp,status)
            values (?,?,?,?,?,?) """, (trid,fcloud_id,parent_cloud_id,1,cur_time,710))
            conn.commit()
            
            return redirect("/transactions")
            
    pos=nx.spring_layout(DG)
    plt.axis('off')
    #nx.draw_networkx(DG, pos)
    nx.draw_networkx(DG,pos, node_size=1500)
    
    gfilename = 'graphs/'+ str(trid)+'-'+str(hcloud_id)+'-graph.png'
    
    #gfilename = "graph"+str(hcloud_id)+".png"
    full_path = "static/"+ gfilename
    plt.savefig("%s" % full_path)
    plt.clf() 
  
    return render_template("graphing.html", gfilename=gfilename, nlist=nlist,elist=elist, trid=trid,
                           home_cloud=home_cloud,parent_cloud=parent_cloud, foreign_cloud=foreign_cloud,obj_trust=obj_trust, 
                           is_xhr= request.is_xhr)

def store_tr_to_db():
    print "This is store to db transaction"
    
    return        "This is store to db transaction"
    
@webapp.route("/addtransaction", methods=["GET", "POST"])      
def add_transaction():
    
    if request.form['bidaction'] == 'Engage':
             
        flash ("Starting a new transaction")
        hcloud_id = request.form['hcloud_id']
        fcloud_id = request.form['fcloud_id']
        comp_trust = request.form['comp_trust']
        
        print "this is add_transaction adding ", hcloud_id, "  +  ", fcloud_id, comp_trust
        cur.execute("insert into transactions(hcloud_id,lastpeer, foreignpeers,comptrust) values(?,?,?,?)", (hcloud_id,fcloud_id,1,comp_trust))
        conn.commit()
    
    #cur.execute("select * from transactions where "
    
    # This will redirect to show transaction page which is not available now   
        # Till then it is redirecting to biddings page  
        #
        
    return redirect("/biddings")
    
@webapp.route('/transactions', defaults={'sort': 'comptrust', 'order': 'desc'})
@webapp.route('/transactions/<string:sort>')
@webapp.route('/transactions/<string:sort>/<string:order>')
def transactions(sort='id', order='asc'):
    
    #check_login()
#     query1= """select ID,hcloud_id, (select cname from cprofile where id=hcloud_id) as 'homecloud', 
#              (select cname from cprofile where id=lastpeer) as 'lastpeer', foreignpeers, 
#              strftime('%d-%m-%Y',creationtime),strftime('%H:%M:%S',creationtime),
#              strftime('%d-%m-%Y',lastactivity),strftime('%H:%M:%S',lastactivity),
#              tthreshold, comptrust,  
#              (select statuscodes.description from statuscodes where statuscodes.code=transactions.status), 
#              (select cprofile.cname from cprofile where cprofile.id=(select subtrans.parent_id from subtrans """  
#                  
#     query2 ="where cloud_id=%s))" % (session['reg_clouds'][0][0]) 
#                  
#     query3 = "from transactions where transactions.ID = (select subtrans.transaction_id from subtrans "   
#     query4 =  "where cloud_id=%s)" % (session['reg_clouds'][0][0]) 
#     query5 = " order by " + sort + ' ' + order 
#     
    
    query1= """select ID,hcloud_id, (select cname from cprofile where id=hcloud_id) as 'homecloud', 
             (select cname from cprofile where id=lastpeer) as 'lastpeer', foreignpeers, 
             strftime('%d-%m-%Y',creationtime),strftime('%H:%M:%S',creationtime),
             strftime('%d-%m-%Y',lastactivity),strftime('%H:%M:%S',lastactivity),
             tthreshold, comptrust,  
             (select statuscodes.description from statuscodes where statuscodes.code=transactions.status) 
              """  
    query2 = "from transactions  "   
    query3 =  "where hcloud_id=%s" % (session['reg_clouds'][0][0]) 
    query4 = " order by " + sort + ' ' + order 
    
    #print query1, query2 , query3, query4
    cur.execute (query1+query2+query3+query4)
    transactions = cur.fetchall()
    print (session['reg_clouds'][0][0])
    
    query11= """select subtrans.transaction_id, 
                transactions.hcloud_id, 
                (select cname from cprofile where id =transactions.hcloud_id) as 'Home cloud', 
                 
                subtrans.parent_id, (select cname from cprofile where id =parent_id) as 'Parent Name',
                transactions.foreignpeers, 
                transactions.creationtime, transactions.lastactivity, 
                transactions.tthreshold, transactions.comptrust, 
                (select description from statuscodes where code = subtrans.status) as 'Status'                 
                from subtrans 
                inner join transactions on transactions.ID=subtrans.transaction_id where cloud_id=%s""" % (session['reg_clouds'][0][0]) 
        
    cur.execute(query11)
    subtrans = cur.fetchall()
    
    
    return render_template('transactions.html', transactions=transactions,subtrans=subtrans, 
                            sort=sort, order=order, page='transactions', is_xhr=request.is_xhr)
    
@webapp.route('/transaction/<int:trid>', defaults={'section': 'overview'})
@webapp.route('/transaction/<string:triid>', defaults={'section': 'overview'})
@webapp.route('/transaction/<int:trid>/<string:section>')
def transaction(trid, section):
    #print "This is profile function", cid
    valid_sections = [
        'overview',
        'objective',
        'subjective',
        ]

    if section not in valid_sections:
        errmsg = 'Invalid subsection when trying to view detail' 
        return render_template('error.html', error=errmsg), 404
    
    
    query11= """select subtrans.transaction_id, 
            transactions.hcloud_id, (select cname from cprofile where id =transactions.hcloud_id) as 'Home cloud',
            subtrans.cloud_id, (select cname from cprofile where id =cloud_id) as 'My Name', 
            subtrans.parent_id, (select cname from cprofile where id =parent_id) as 'Parent Name',
            subtrans.timestamp, 
            subtrans.resource_id, (select resources.rname from resources where resources.id=subtrans.resource_id) as 'Resource', 
            transactions.creationtime, transactions.lastactivity, transactions.tthreshold, transactions.comptrust, 
            subtrans.status, (select description from statuscodes where code = subtrans.status) as 'Status'                
            
            from subtrans inner join transactions on transactions.ID=subtrans.transaction_id where transactions.ID=%s""" % (trid)
    cur.execute(query11)
    transaction_data = cur.fetchall()
    
    print transaction_data
    
    
    context = {
        'transaction_data' : transaction_data, 
        'section': section,
        'page': 'transactions',
        'is_xhr': request.is_xhr
    }
    
    if section ==  'objective':

        context['objective'] = 'This is objective trust'
       
        
    elif section == 'subjective':
        context['subjective'] = 'This is subjective trust'
       
    return render_template(
        'transaction/%s.html' % section,
        **context
    ) 
    