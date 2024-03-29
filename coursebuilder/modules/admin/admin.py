# Copyright 2015 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS-IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Site administration functionality."""

__author__ = 'Pavel Simakov (psimakov@google.com)'

import cgi
import cStringIO
import datetime
import os
import sys
import time
import urllib

import messages

import appengine_config
from common import jinja_utils
from common import safe_dom
from common import tags
from common import users
from common import utils as common_utils
from controllers import sites
from controllers.utils import ApplicationHandler
from controllers.utils import ReflectiveRequestHandler
import models
from models import config
from models import counters
from models import courses
from models import custom_modules
from models import roles
from models.config import ConfigProperty
import modules.admin.config
from modules.admin.config import ConfigPropertyEditor
from modules.dashboard import dashboard
from modules.dashboard import tabs
from modules.oeditor import oeditor

import google.appengine.api.app_identity as app

RESOURCES_PATH = '/modules/admin/resources'

TEMPLATE_DIR = os.path.join(appengine_config.BUNDLE_ROOT, 'modules', 'admin')

DIRECT_CODE_EXECUTION_UI_ENABLED = False

# A time this module was initialized.
BEGINNING_OF_TIME = time.time()

DELEGATED_ACCESS_IS_NOT_ALLOWED = """
You must be an actual admin user to continue.
Users with the delegated admin rights are not allowed."""


def escape(text):
    """Escapes HTML in text."""
    if text:
        return cgi.escape(text)
    return text


def evaluate_python_code(code):
    """Compiles and evaluates a Python script in a restricted environment."""

    code = code.replace('\r\n', '\n')

    save_stdout = sys.stdout
    results_io = cStringIO.StringIO()
    try:
        sys.stdout = results_io
        try:
            compiled_code = compile(code, '<string>', 'exec')
            exec(compiled_code, globals())  # pylint: disable=exec-used
        except Exception as e:  # pylint: disable=broad-except
            results_io.write('Error: %s' % e)
            return results_io.getvalue(), False
    finally:
        sys.stdout = save_stdout
    return results_io.getvalue(), True


class WelcomeHandler(ApplicationHandler, ReflectiveRequestHandler):
    default_action = 'welcome'
    get_actions = [default_action]
    post_actions = ['explore_sample', 'add_first_course', 'configure_settings']

    # Enable other modules to make changes to sample course import.
    # Each member must be a function of the form:
    #     callback(course, errors)
    COPY_SAMPLE_COURSE_HOOKS = []

    # Enable other modules to put global warnings on the welcome page.  This
    # is useful when you want to ask for permission from the installation
    # administrator, and you want to be absolutely certain the administrator
    # has seen the request.  Items appended here must be callable, taking
    # no parameters.  The return value will be inserted onto the welcome.html
    # page; see the loop adding 'item_form_content' to the page.
    WELCOME_FORM_HOOKS = []

    # Items on this list are called back when the welcome page has been
    # submitted.  These should take two parameters: the course just created
    # and the page handler object.
    POST_HOOKS = []

    def get_template(self, template_name):
        return jinja_utils.get_template(template_name, [TEMPLATE_DIR])

    def can_view(self):
        """Checks if current user has viewing rights."""
        action = self.request.get('action')
        if action == 'add_first_course':
            return modules.admin.config.CoursesPropertyRights.can_add()
        return roles.Roles.is_super_admin()

    def can_edit(self):
        """Checks if current user has editing rights."""
        return self.can_view()

    def get(self):
        user = users.get_current_user()
        if not user:
            self.redirect(
                users.create_login_url('/admin/welcome'), normalize=False)
            return
        if not self.can_view():
            return
        super(WelcomeHandler, self).get()

    def post(self):
        if not self.can_edit():
            return
        app_context = super(WelcomeHandler, self).post()
        common_utils.run_hooks(self.POST_HOOKS, app_context, self)

    def _redirect(self, app_context, url):
        self.app_context = app_context
        self.redirect(url)

    def get_welcome(self):
        template_values = {}
        template_values['version'] = os.environ['GCB_PRODUCT_VERSION']
        template_values['course_count'] = len(sites.get_all_courses())
        template_values['add_first_xsrf'] = self.create_xsrf_token(
            'add_first_course')
        template_values['explore_sample_xsrf'] = self.create_xsrf_token(
            'explore_sample')
        template_values['configure_settings_xsrf'] = self.create_xsrf_token(
            'configure_settings')
        template_values['global_admin_url'] = GlobalAdminHandler.LINK_URL
        welcome_form_content = []
        for hook in self.WELCOME_FORM_HOOKS:
            welcome_form_content.append(hook())
        template_values['welcome_form_content'] = welcome_form_content
        self.response.write(
            self.get_template('welcome.html').render(template_values))

    def _make_new_course(self, uid, title):
        """Make a new course entry."""
        errors = []
        admin_email = users.get_current_user().email()
        entry = sites.add_new_course_entry(
            uid, title, admin_email, errors)
        if errors:
            raise Exception(errors)
        app_context = sites.get_all_courses(entry)[0]
        new_course = models.courses.Course(None, app_context=app_context)
        new_course.init_new_course_settings(title, admin_email)
        return app_context

    def _copy_sample_course(self, uid):
        """Make a fresh copy of sample course."""
        src_app_context = sites.get_all_courses('course:/:/:')[0]
        dst_app_context = self._make_new_course(uid, '%s (%s)' % (
            src_app_context.get_title(), os.environ['GCB_PRODUCT_VERSION']))
        errors = []
        dst_course = courses.Course(None, dst_app_context)
        dst_course.import_from(src_app_context, errors)
        dst_course.save()
        if not errors:
            common_utils.run_hooks(
                self.COPY_SAMPLE_COURSE_HOOKS, dst_app_context, errors)
        if errors:
            raise Exception(errors)
        return dst_app_context

    def post_explore_sample(self):
        """Navigate to or import sample course."""
        course = None
        for uid in ['sample', 'sample_%s' % os.environ[
            'GCB_PRODUCT_VERSION'].replace('.', '_')]:
            course = sites.get_course_index(
                ).get_app_context_for_namespace('ns_%s' % uid)
            if not course:
                course = self._copy_sample_course(uid)
                break
        assert course is not None
        self._redirect(course, '/dashboard')
        return course

    def post_add_first_course(self):
        """Adds first course to the deployment."""
        uid = 'first'
        course = sites.get_course_index().get_course_for_path('/%s' % uid)
        if course:
            self._redirect(course, '/dashboard')
            return course
        course = self._make_new_course(uid, 'My First Course')
        self._redirect(course, '/dashboard')
        return course

    def post_configure_settings(self):
        self.redirect('/admin/global')


class BaseAdminHandler(ConfigPropertyEditor):
    """Base class holding methods required for administration of site."""

    ACTION = 'admin'
    DEFAULT_TAB = 'courses'

    def get_default_tab_action(self):
        return 'admin'

    @classmethod
    def bind_tabs(cls):

        def bind(key, label, handler, href=None):
            if href:
                target = '_blank'
            else:
                href = 'admin?action=admin&tab=%s' % key
                target = None

            tabs.Registry.register(
                cls.ACTION, key, label, contents=handler, href=href,
                target=target)

        bind('courses', 'Courses', cls.get_courses)
        bind('settings', 'Site Settings', cls.get_settings)
        bind('perf', 'Metrics', cls.get_perf)
        bind('deployment', 'Deployment', cls.get_deployment)

        if DIRECT_CODE_EXECUTION_UI_ENABLED:
            bind('console', 'Console', cls.get_console)

        if appengine_config.gcb_appstats_enabled():
            bind('stats', 'Appstats', None, href='/admin/stats/')

        if appengine_config.PRODUCTION_MODE:
            app_id = app.get_application_id()
            href = (
                'https://appengine.google.com/'
                'dashboard?app_id=s~%s' % app_id)
            bind('gae', 'Google App Engine', None, href=href)
        else:
            bind(
                 'gae', 'Google App Engine', None,
                 href='http://localhost:8000/')
        bind('welcome', 'Welcome', None, href='/admin/welcome')
        bind(
             'help', 'Site Help', None,
             href='https://code.google.com/p/course-builder/wiki/AdminPage')
        bind(
             'news', 'News', None,
             href=(
                'https://groups.google.com/forum/'
                '?fromgroups#!forum/course-builder-announce'))

    @classmethod
    def unbind_tabs(cls):
        tabs.Registry.unregister_group(cls.ACTION)

    @classmethod
    def bind_get_actions(cls):
        cls.get_actions.append(cls.ACTION)
        cls.get_actions.append('add_course')
        cls.get_actions.append('config_edit')

    @classmethod
    def unbind_get_actions(cls):
        cls.get_actions.remove(cls.ACTION)
        cls.get_actions.remove('add_course')
        cls.get_actions.remove('config_edit')

    @classmethod
    def bind_post_actions(cls):
        cls.post_actions.append('config_override')
        cls.post_actions.append('config_reset')

        if DIRECT_CODE_EXECUTION_UI_ENABLED:
            cls.post_actions.append('console_run')

    @classmethod
    def unbind_post_actions(cls):
        cls.post_actions.remove('config_override')
        cls.post_actions.remove('config_reset')

        if DIRECT_CODE_EXECUTION_UI_ENABLED:
            cls.post_actions.remove('console_run')

    def can_view(self, unused_action):
        """Checks if current user has viewing rights."""
        # Overrides method in DashboardHandler
        action = self.request.get('action')
        if action == 'add_course':
            return modules.admin.config.CoursesPropertyRights.can_add()
        return roles.Roles.is_super_admin()

    def can_edit(self):
        """Checks if current user has editing rights."""
        # Overrides method in DashboardHandler
        return self.can_view(self.ACTION)

    def get_course_picker(self, destination=None):
        return super(BaseAdminHandler, self).get_course_picker(
            destination='/admin')

    def get_admin(self):
        """The main entry point to the admin console."""
        tab = tabs.Registry.get_tab(
            self.ACTION, self.request.get('tab') or self.DEFAULT_TAB)
        if tab:
            tab.contents(self)
        else:
            self.error(404)

    def render_dict(self, source_dict, title):
        """Renders a dictionary ordered by keys."""
        keys = sorted(source_dict.keys())

        content = safe_dom.NodeList()
        content.append(safe_dom.Element('h3').add_text(title))
        ol = safe_dom.Element('ol')
        content.append(ol)
        for key in keys:
            value = source_dict[key]
            if isinstance(value, ConfigProperty):
                value = value.value
            ol.add_child(
                safe_dom.Element('li').add_text('%s: %s' % (key, value)))
        return content

    def get_perf(self):
        """Shows server performance counters page."""
        template_values = {}
        template_values['page_title'] = self.format_title('Metrics')
        template_values['page_description'] = messages.METRICS_DESCRIPTION

        perf_counters = {}

        # built in counters
        perf_counters['gcb-admin-uptime-sec'] = long(
            time.time() - BEGINNING_OF_TIME)

        # config counters
        perf_counters['gcb-config-overrides'] = len(
            config.Registry.get_overrides())
        perf_counters['gcb-config-age-sec'] = (
            long(time.time()) - config.Registry.last_update_time)
        perf_counters['gcb-config-update-time-sec'] = (
            config.Registry.last_update_time)
        perf_counters['gcb-config-update-index'] = config.Registry.update_index

        # add all registered counters
        all_counters = counters.Registry.registered.copy()
        for name in all_counters.keys():
            global_value = all_counters[name].global_value
            if not global_value:
                global_value = 'NA'
            perf_counters[name] = '%s / %s' % (
                all_counters[name].value, global_value)

        template_values['main_content'] = self.render_dict(
            perf_counters, 'In-process Performance Counters (local/global)')
        self.render_page(template_values)

    def _make_routes_dom(self, parent_element, routes, caption):
        """Renders routes as DOM."""
        if routes:
            # sort routes
            all_routes = []
            for route in routes:
                if route:
                    all_routes.append(str(route))

            # render as DOM
            ul = safe_dom.Element('ul')
            parent_element.add_child(ul)

            ul.add_child(safe_dom.Element('li').add_text(caption))
            ul2 = safe_dom.Element('ul')
            ul.add_child(ul2)
            for route in sorted(all_routes):
                if route:
                    ul2.add_child(safe_dom.Element('li').add_text(route))

    def get_deployment(self):
        """Shows server environment and deployment information page."""
        template_values = {}
        template_values['page_title'] = self.format_title('Deployment')
        template_values['page_description'] = messages.DEPLOYMENT_DESCRIPTION

        # modules
        module_content = safe_dom.NodeList()
        module_content.append(
            safe_dom.Element('h3').add_text('Modules'))
        ol = safe_dom.Element('ol')
        module_content.append(ol)
        for name in sorted(custom_modules.Registry.registered_modules.keys()):
            enabled_text = ''
            if name not in custom_modules.Registry.enabled_module_names:
                enabled_text = ' (disabled)'

            li = safe_dom.Element('li').add_text('%s%s' % (name, enabled_text))
            ol.add_child(li)

            amodule = custom_modules.Registry.registered_modules.get(name)
            self._make_routes_dom(
                li, amodule.global_routes, 'Global Routes')
            self._make_routes_dom(
                li, amodule.namespaced_routes, 'Namespaced Routes')

        # Custom tags.
        tag_content = safe_dom.NodeList()
        tag_content.append(
            safe_dom.Element('h3').add_text('Custom Tags'))
        ol = safe_dom.Element('ol')
        tag_content.append(ol)
        tag_bindings = tags.get_tag_bindings()
        for name in sorted(tag_bindings.keys()):
            clazz = tag_bindings.get(name)
            tag = clazz()
            vendor = tag.vendor()
            ol.add_child(safe_dom.Element('li').add_text(
                '%s: %s: %s' % (name, tag.__class__.__name__, vendor)))

        # Yaml file content.
        yaml_content = safe_dom.NodeList()
        yaml_content.append(
            safe_dom.Element('h3').add_text('Contents of ').add_child(
                safe_dom.Element('code').add_text('app.yaml')))
        ol = safe_dom.Element('ol')
        yaml_content.append(ol)
        yaml_lines = open(os.path.join(os.path.dirname(
            __file__), '../../app.yaml'), 'r').readlines()
        for line in yaml_lines:
            ol.add_child(safe_dom.Element('li').add_text(line))

        # Application identity and users service information.
        app_id = app.get_application_id()
        app_dict = {}
        app_dict['application_id'] = escape(app_id)
        app_dict['default_ver_hostname'] = escape(
            app.get_default_version_hostname())
        app_dict['users_service_name'] = escape(
            users.UsersServiceManager.get().get_service_name())

        template_values['main_content'] = safe_dom.NodeList().append(
            self.render_dict(app_dict, 'About the Application')
        ).append(
            module_content
        ).append(
            tag_content
        ).append(
            yaml_content
        ).append(
            self.render_dict(os.environ, 'Server Environment Variables'))

        self.render_page(template_values)

    def get_settings(self):
        """Shows configuration properties information page."""
        template_values = {}
        template_values['page_title'] = self.format_title('Settings')
        template_values['page_description'] = messages.SETTINGS_DESCRIPTION

        content = safe_dom.NodeList()
        content.append(safe_dom.Element(
            'link', rel='stylesheet',
            href='/modules/admin/resources/css/admin.css'))
        table = safe_dom.Element('table', className='gcb-config').add_child(
            safe_dom.Element('tr').add_child(
                safe_dom.Element('th').add_text('Name')
            ).add_child(
                safe_dom.Element('th').add_text('Current Value')
            ).add_child(
                safe_dom.Element('th').add_text('Actions')
            ).add_child(
                safe_dom.Element('th').add_text('Description')
            ))
        content.append(
            safe_dom.Element('h3').add_text('All Settings')
        ).append(table)

        def get_style_for(value, value_type):
            """Formats CSS style for given value."""
            style = ''
            if not value or value_type in [int, long, bool]:
                style = 'text-align: center;'
            return style

        def get_action_html(caption, args, onclick=None, idName=None):
            """Formats actions <a> link."""
            a = safe_dom.Element(
                'a', href='%s?%s' % (
                    self.LINK_URL, urllib.urlencode(args)),
                className='gcb-button'
            ).add_text(caption)
            if onclick:
                a.add_attribute(onclick=onclick)
            if idName:
                a.add_attribute(id=idName)
            return a

        def get_actions(name, override):
            """Creates actions appropriate to an item."""
            if override:
                return get_action_html('Edit', {
                    'action': 'config_edit', 'name': name}, idName=name)
            else:
                return safe_dom.Element(
                    'form',
                    action='%s?%s' % (
                        self.LINK_URL,
                        urllib.urlencode(
                            {'action': 'config_override', 'name': name})),
                    method='POST'
                ).add_child(
                    safe_dom.Element(
                        'input', type='hidden', name='xsrf_token',
                        value=self.create_xsrf_token('config_override'))
                ).add_child(
                    safe_dom.Element(
                        'button', className='gcb-button', type='submit', id=name
                    ).add_text('Override'))

        def get_doc_string(item, default_value):
            """Formats an item documentation string for display."""
            doc_string = item.doc_string
            if not doc_string:
                doc_string = 'No documentation available.'
            if isinstance(doc_string, safe_dom.NodeList) or isinstance(
                    doc_string, safe_dom.Node):
                return safe_dom.NodeList().append(doc_string).append(
                    safe_dom.Text(' Default: \'%s\'.' % default_value))
            doc_string = ' %s Default: \'%s\'.' % (doc_string, default_value)
            return safe_dom.Text(doc_string)

        def get_lines(value):
            """Convert \\n line breaks into <br> and escape the lines."""
            escaped_value = safe_dom.NodeList()
            for line in str(value).split('\n'):
                escaped_value.append(
                    safe_dom.Text(line)).append(safe_dom.Element('br'))
            return escaped_value

        # get fresh properties and their overrides
        unused_overrides = config.Registry.get_overrides(force_update=True)
        registered = config.Registry.registered.copy()
        db_overrides = config.Registry.db_overrides.copy()
        names_with_draft = config.Registry.names_with_draft.copy()

        count = 0
        for name in sorted(registered.keys()):
            count += 1
            item = registered[name]
            has_environ_value, unused_environ_value = item.get_environ_value()

            # figure out what kind of override this is
            class_current = ''
            if has_environ_value:
                class_current = 'gcb-env-diff'
            if item.name in db_overrides:
                class_current = 'gcb-db-diff'
            if item.name in names_with_draft:
                class_current = 'gcb-db-draft'

            # figure out default and current value
            default_value = item.default_value
            value = item.value
            if default_value:
                default_value = str(default_value)
            if value:
                value = str(value)

            style_current = get_style_for(value, item.value_type)

            tr = safe_dom.Element('tr')
            table.add_child(tr)

            tr.add_child(
                safe_dom.Element(
                    'td', style='white-space: nowrap;').add_text(item.name))

            td_value = safe_dom.Element('td').add_child(get_lines(value))
            if style_current:
                td_value.add_attribute(style=style_current)
            if class_current:
                td_value.add_attribute(className=class_current)
            tr.add_child(td_value)

            tr.add_child(
                safe_dom.Element(
                    'td', style='white-space: nowrap;', align='center'
                ).add_child(get_actions(
                    name, name in db_overrides or name in names_with_draft)))

            tr.add_child(
                safe_dom.Element(
                    'td').add_child(get_doc_string(item, default_value)))

        table.add_child(
            safe_dom.Element('tr').add_child(
                safe_dom.Element(
                    'td', colspan='4', align='right'
                ).add_text('Total: %s item(s)' % count)))

        content.append(
            safe_dom.Element('p').add_child(
                safe_dom.Element('strong').add_text('Legend')
            ).add_text(':').add_text("""
                For each property, the value shown corresponds to, in
                descending order of priority:
            """).add_child(
                safe_dom.Element('span', className='gcb-db-diff').add_child(
                    safe_dom.Entity('&nbsp;')
                ).add_text(
                    '[ the value override set via this page ]'
                ).add_child(safe_dom.Entity('&nbsp;'))
            ).add_text(', ').add_child(
                safe_dom.Element('span', className='gcb-db-draft').add_child(
                    safe_dom.Entity('&nbsp;')
                ).add_text(
                    '[ the default value with pending value override ]'
                ).add_child(safe_dom.Entity('&nbsp;'))
            ).add_text(', ').add_child(
                safe_dom.Element('span', className='gcb-env-diff').add_child(
                    safe_dom.Entity('&nbsp;')
                ).add_text(
                    '[ the environment value in app.yaml ]'
                ).add_child(safe_dom.Entity('&nbsp;'))
            ).add_text(', ').add_text("""
                and the [ default value ] in the Course Builder codebase.
            """))

        template_values['main_content'] = content
        self.render_page(template_values)

    def get_courses(self):
        """Shows a list of all courses available on this site."""
        template_values = {}
        template_values['page_title'] = self.format_title('Courses')
        template_values['page_description'] = messages.COURSES_DESCRIPTION

        content = safe_dom.NodeList()
        content.append(
            safe_dom.Element(
                'a', id='add_course', className='gcb-button gcb-pull-right',
                role='button', href='%s?action=add_course' % self.LINK_URL
            ).add_text('Add Course')
        ).append(
            safe_dom.Element('div', style='clear: both; padding-top: 2px;')
        ).append(
            safe_dom.Element('h3').add_text('All Courses')
        )
        table = safe_dom.Element('table')
        content.append(table)
        table.add_child(
            safe_dom.Element('tr').add_child(
                safe_dom.Element('th').add_text('Course Title')
            ).add_child(
                safe_dom.Element('th').add_text('Context Path')
            ).add_child(
                safe_dom.Element('th').add_text('Content Location')
            ).add_child(
                safe_dom.Element('th').add_text('Student Data Location')
            )
        )
        count = 0
        for course in sorted(
            sites.get_all_courses(),
            key=lambda course: course.get_title().lower()):
            count += 1
            error = safe_dom.Text('')
            slug = course.get_slug()
            name = course.get_title()

            if course.fs.is_read_write():
                location = 'namespace: %s' % course.get_namespace_name()
            else:
                location = 'disk: %s' % sites.abspath(
                    course.get_home_folder(), '/')

            if slug == '/':
                link = '/dashboard'
            else:
                link = '%s/dashboard' % slug
            link = safe_dom.Element('a', href=link).add_text(name)

            table.add_child(
                safe_dom.Element('tr').add_child(
                    safe_dom.Element('td').add_child(link).add_child(error)
                ).add_child(
                    safe_dom.Element('td').add_text(slug)
                ).add_child(
                    safe_dom.Element('td').add_text(location)
                ).add_child(
                    safe_dom.Element('td').add_text(
                        'namespace: %s' % course.get_namespace_name())
                ))

        table.add_child(
            safe_dom.Element('tr').add_child(
                safe_dom.Element('td', colspan='4', align='right').add_text(
                    'Total: %s item(s)' % count)))
        template_values['main_content'] = content

        self.render_page(template_values)

    def get_console(self):
        """Shows interactive Python console page."""
        template_values = {}
        template_values['page_title'] = self.format_title('Console')

        # Check rights.
        if not roles.Roles.is_direct_super_admin():
            template_values['main_content'] = DELEGATED_ACCESS_IS_NOT_ALLOWED
            self.render_page(template_values)
            return

        content = safe_dom.NodeList()
        content.append(
            safe_dom.Element('p').add_child(
                safe_dom.Element('i').add_child(
                    safe_dom.Element('strong').add_text('WARNING!')
                ).add_text("""
 The Interactive Console has the same
access to the application's environment and services as a .py file
inside the application itself. Be careful, because this means writes
to your data store will be executed for real!""")
            )
        ).append(
            safe_dom.Element('p').add_child(
                safe_dom.Element('strong').add_text("""
Input your Python code below and press "Run Program" to execute.""")
            )
        ).append(
            safe_dom.Element(
                'form',
                action='%s?action=console_run' % self.LINK_URL,
                method='POST'
            ).add_child(
                safe_dom.Element(
                    'input', type='hidden', name='xsrf_token',
                    value=self.create_xsrf_token('console_run'))
            ).add_child(
                safe_dom.Element(
                    'textarea', style='width: 95%; height: 200px;',
                    name='code')
            ).add_child(
                safe_dom.Element('p', align='center').add_child(
                    safe_dom.Element(
                        'button', className='gcb-button', type='submit'
                    ).add_text('Run Program')
                )
            )
        )

        template_values['main_content'] = content
        self.render_page(template_values)

    def post_console_run(self):
        """Executes dynamically submitted Python code."""
        template_values = {}
        template_values['page_title'] = self.format_title('Execution Results')

        # Check rights.
        if not roles.Roles.is_direct_super_admin():
            template_values['main_content'] = DELEGATED_ACCESS_IS_NOT_ALLOWED
            self.render_page(template_values)
            return

        # Execute code.
        code = self.request.get('code')
        time_before = time.time()
        output, results = evaluate_python_code(code)
        duration = long(time.time() - time_before)
        status = 'FAILURE'
        if results:
            status = 'SUCCESS'

        # Render results.
        content = safe_dom.NodeList()
        content.append(
            safe_dom.Element('h3').add_text('Submitted Python Code'))
        ol = safe_dom.Element('ol')
        content.append(ol)
        for line in code.split('\n'):
            ol.add_child(safe_dom.Element('li').add_text(line))

        content.append(
            safe_dom.Element('h3').add_text('Execution Results')
        ).append(
            safe_dom.Element('ol').add_child(
                safe_dom.Element('li').add_text('Status: %s' % status)
            ).add_child(
                safe_dom.Element('li').add_text('Duration (sec): %s' % duration)
            )
        ).append(
            safe_dom.Element('h3').add_text('Program Output')
        ).append(
            safe_dom.Element('blockquote').add_child(
                safe_dom.Element('pre').add_text(output))
        )

        template_values['main_content'] = content
        self.render_page(template_values, in_tab='console')


class AdminHandler(BaseAdminHandler, dashboard.DashboardHandler):
    """Handler to present admin settings in namespaced context."""

    # The binding URL for this handler
    URL = '/admin'

    # The URL used in relative addreses of this handler
    LINK_URL = 'admin'

    def format_title(self, text):
        return super(AdminHandler, self).format_title(
            'Admin > %s' % text)

    def get_template(self, template_name, dirs):
        return super(AdminHandler, self).get_template(
            template_name, [TEMPLATE_DIR] + dirs)

    def render_page(self, template_values, in_action=None, in_tab=None):
        in_action = in_action or self.ACTION
        in_tab = in_tab or self.request.get('tab') or self.DEFAULT_TAB

        super(AdminHandler, self).render_page(
            template_values, in_action=in_action, in_tab=in_tab)


class GlobalAdminHandler(
        BaseAdminHandler, ApplicationHandler, ReflectiveRequestHandler):
    """Handler to present admin settings in global context."""

    # The binding URL for this handler
    URL = '/admin/global'

    # The URL used in relative addreses of this handler
    LINK_URL = '/admin/global'

    # List of functions which are used to generate content displayed at the top
    # of every dashboard page. Use this with caution, as it is extremely
    # invasive of the UX. Each function receives the handler as arg and returns
    # an object to be inserted into a Jinja template (e.g. a string, a safe_dom
    # Node or NodeList, or a jinja2.Markup).
    PAGE_HEADER_HOOKS = []

    default_action = 'admin'
    get_actions = [default_action]
    post_actions = []

    def format_title(self, text):
        return 'Course Builder > Admin > %s' % text

    def _get_top_nav(self, in_action, in_tab):
        nav_bars = []

        nav = safe_dom.NodeList()

        nav.append(safe_dom.Element(
            'a', href=self.URL, className='selected'
        ).add_text('Site Admin'))

        nav.append(safe_dom.Element(
            'a',
            href='https://code.google.com/p/course-builder/wiki/Dashboard',
            target='_blank'
        ).add_text('Help'))

        nav.append(safe_dom.Element(
            'a',
            href=(
                'https://groups.google.com/forum/?fromgroups#!categories/'
                'course-builder-forum/general-troubleshooting'),
            target='_blank'
        ).add_text('Support'))
        nav_bars.append(nav)

        tab_name = in_tab or self.request.get('tab') or self.DEFAULT_TAB
        sub_nav = safe_dom.NodeList()
        tab_group = tabs.Registry.get_tab_group(self.default_action)
        for tab in tab_group:
            if tab.contents:
                href = '%s?tab=%s' % (self.LINK_URL, tab.name)
            else:
                href = tab.href
            target = tab.target or '_self'
            sub_nav.append(
                safe_dom.A(
                    href,
                    className=('selected' if tab.name == tab_name else ''),
                    target=target)
                .add_text(tab.title))
        nav_bars.append(sub_nav)

        return nav_bars

    def get(self):
        tab = self.request.get('tab')

        if tab:
            destination = '%s?tab=%s' % (self.LINK_URL, tab)
        else:
            destination = self.LINK_URL

        user = users.get_current_user()
        if not user:
            self.redirect(users.create_login_url(destination), normalize=False)
            return
        if not self.can_view(self.ACTION):
            if appengine_config.PRODUCTION_MODE:
                self.error(403)
            else:
                self.redirect(
                    users.create_login_url(destination), normalize=False)
            return

        # Force reload of properties. It's expensive, but admin deserves it!
        config.Registry.get_overrides(force_update=True)

        super(GlobalAdminHandler, self).get()

    def post(self):
        if not self.can_edit():
            self.redirect('/', normalize=False)
            return
        return super(GlobalAdminHandler, self).post()

    def get_template(self, template_name, dirs):
        """Sets up an environment and Gets jinja template."""
        dashboard_template_dir = os.path.join(
            appengine_config.BUNDLE_ROOT, 'modules', 'dashboard')
        return jinja_utils.get_template(
            template_name, dirs + [dashboard_template_dir], handler=self)

    def render_page(self, template_values, in_action=None, in_tab=None):
        page_title = template_values['page_title']
        template_values['header_title'] = page_title
        template_values['page_headers'] = [
            hook(self) for hook in self.PAGE_HEADER_HOOKS]
        template_values['breadcrumbs'] = page_title

        template_values['top_nav'] = self._get_top_nav(in_action, in_tab)
        template_values['gcb_course_base'] = '/'
        template_values['user_nav'] = safe_dom.NodeList().append(
            safe_dom.Text('%s | ' % users.get_current_user().email())
        ).append(
            safe_dom.Element(
                'a', href=users.create_logout_url(self.request.uri)
            ).add_text('Logout'))
        template_values[
            'page_footer'] = 'Page created on: %s' % datetime.datetime.now()
        template_values['coursebuilder_version'] = (
            os.environ['GCB_PRODUCT_VERSION'])
        template_values['application_id'] = app.get_application_id()
        template_values['application_version'] = (
            os.environ['CURRENT_VERSION_ID'])
        template_values['can_highlight_code'] = oeditor.CAN_HIGHLIGHT_CODE.value
        if not template_values.get('sections'):
            template_values['sections'] = []

        self.response.write(
            self.get_template('view.html', []).render(template_values))


def notify_module_enabled():
    BaseAdminHandler.bind_tabs()
    AdminHandler.bind_get_actions()
    AdminHandler.bind_post_actions()
    GlobalAdminHandler.bind_get_actions()
    GlobalAdminHandler.bind_post_actions()


def notify_module_disabled():
    BaseAdminHandler.unbind_tabs()
    AdminHandler.unbind_get_actions()
    AdminHandler.unbind_post_actions()
    GlobalAdminHandler.unbind_get_actions()
    GlobalAdminHandler.unbind_post_actions()


custom_module = None


def register_module():
    """Registers this module in the registry."""

    global_handlers = [
        (GlobalAdminHandler.URL, GlobalAdminHandler),
        ('/admin/welcome', WelcomeHandler),
        ('/rest/config/item', (
            modules.admin.config.ConfigPropertyItemRESTHandler)),
        ('/rest/courses/item', modules.admin.config.CoursesItemRESTHandler),
        (os.path.join(RESOURCES_PATH, '.*'), tags.ResourcesHandler)]

    namespaced_handlers = [(AdminHandler.URL, AdminHandler)]

    global custom_module  # pylint: disable=global-statement
    custom_module = custom_modules.Module(
        'Site Admin',
        'A set of pages for Course Builder site administrator.',
        global_handlers, namespaced_handlers,
        notify_module_enabled=notify_module_enabled,
        notify_module_disabled=notify_module_disabled)
    return custom_module
