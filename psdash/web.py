# coding=utf-8
import logging
import psutil
import socket
from datetime import datetime, timedelta
import uuid
import locale
import os
import openpyxl
from openpyxl import load_workbook, Workbook  
from openpyxl.compat import range
from openpyxl.cell import cell
from openpyxl.utils import get_column_letter
import re
#import flask_excel as fe
#import pyexcel as pe 

from flask import Flask, render_template, request, session, jsonify, Response, Blueprint, current_app, g, flash, redirect, url_for
from flask_wtf import FlaskForm
from wtforms import Form,TextField,TextAreaField,validators,StringField,SubmitField, FileField
from werkzeug.local import LocalProxy
from werkzeug.utils import secure_filename 
from psdash.helpers import socket_families, socket_types

import sqlite3

from fileinput import filename
from pyexcel.plugins.sources.params import FILE_NAME
from pyexcel.internal.sheets.row import Row


UPLOAD_FOLDER = '/static/uploads'
ALLOWED_EXTENSIONS = set(['txt', 'pdf', 'png', 'jpg', 'jpeg', 'gif', 'xlsx'])

logger = logging.getLogger('psdash.web')
webapp = Blueprint('psdash', __name__, static_folder='static')


#webapp.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER           

conn = sqlite3.connect('db.sqlite3')
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
    sysinfo = current_service.get_sysinfo()
    uptime = timedelta(seconds=sysinfo['uptime'])
    uptime = str(uptime).split('.')[0]
    return {
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


#@webapp.route('/cprofile')
#def cprofile():
 #   cur.execute("SELECT * from cprofile" )  
    


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
    cname = TextField('Name:', validators=[validators.required()])
    cmeta = TextField('Metatext:', validators=[validators.required()])    
    cendpoint = TextField('Endpoint URL:', validators=[validators.required()])
    cinfo = TextField('Description:', validators=[validators.required()])
 
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
           # return redirect('/upload/', cname) # redirects upon success to your homepage.
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
                        qid=ws.cell(row=x,column=1).value
                        ayes=ws.cell(row=x,column=2).value
                        ano = ws.cell(row=x,column=3).value
                        ana = ws.cell(row=x,column=4).value
                        cur.execute("INSERT INTO TempTable (questionID,ayes,ano,ana,cloud_id) VALUES (?,?,?,?,?)", (qid,ayes,ano,ana,svar_cid)  )
                        conn.commit()
                print 'success!!!!'
            flash('File upload success!!')
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
