#    Copyright (c) 2013 Mirantis, Inc.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
import collections

from murano.common.helpers import token_sanitizer
from murano.common import rpc
from murano.db import models
from murano.db import session as db_session


SessionState = collections.namedtuple('SessionState', [
    'OPENED', 'DEPLOYING', 'DEPLOYED', 'DEPLOY_FAILURE', 'DELETING',
    'DELETE_FAILURE'
])(
    OPENED='opened',
    DEPLOYING='deploying',
    DEPLOYED='deployed',
    DEPLOY_FAILURE='deploy failure',
    DELETING='deleting',
    DELETE_FAILURE='delete failure'
)


class SessionServices(object):
    @staticmethod
    def get_sessions(environment_id, state=None):
        """
        Get list of sessions for specified environment

        :param environment_id: Environment Id
        :param state: glazierapi.db.services.environments.EnvironmentStatus
        :return: Sessions for specified Environment, if SessionState is
        not defined all sessions for specified environment is returned.
        """

        unit = db_session.get_session()
        # Here we duplicate logic for reducing calls to database
        # Checks for validation is same as in validate.
        query = unit.query(models.Session).filter(
            #Get all session for this environment
            models.Session.environment_id == environment_id,
            #Only sessions with same version as current env version are valid
        )

        if state:
            #in this state, if state is not specified return in all states
            query = query.filter(models.Session.state == state),

        return query.order_by(models.Session.version.desc(),
                              models.Session.updated.desc()).all()

    @staticmethod
    def create(environment_id, user_id):
        """
        Creates session object for specific environment for specified user.

        :param environment_id: Environment Id
        :param user_id: User Id
        :return: Created session
        """
        unit = db_session.get_session()
        environment = unit.query(models.Environment).get(environment_id)

        session = models.Session()
        session.environment_id = environment.id
        session.user_id = user_id
        session.state = SessionState.OPENED
        # used for checking if other sessions was deployed before this one
        session.version = environment.version
        # all changes to environment is stored here, and translated to
        # environment only after deployment completed
        session.description = environment.description

        with unit.begin():
            unit.add(session)

        return session

    @staticmethod
    def validate(session):
        """
        Session is valid only if no other session for same
        environment was already deployed on in deploying state,

        :param session: Session for validation
        """

        #if other session is deploying now current session is invalid
        unit = db_session.get_session()

        #if environment version is higher then version on which current session
        #is created then other session was already deployed
        current_env = unit.query(models.Environment).\
            get(session.environment_id)
        if current_env.version > session.version:
            return False

        #if other session is deploying now current session is invalid
        other_is_deploying = unit.query(models.Session).filter_by(
            environment_id=session.environment_id, state=SessionState.DEPLOYING
        ).count() > 0
        if session.state == SessionState.OPENED and other_is_deploying:
            return False

        return True

    @staticmethod
    def deploy(session, unit, token):
        """
        Prepares environment for deployment and send deployment command to
        orchestration engine

        :param session: session that is going to be deployed
        :param unit: SQLalchemy session
        :param token: auth token that is going to be used by orchestration
        """

        #Set X-Auth-Token for conductor
        environment = unit.query(models.Environment).get(
            session.environment_id)

        deleted = session.description['Objects'] is None
        action = None
        if not deleted:
            action = {
                'object_id': environment.id,
                'method': 'deploy',
                'args': {}
            }

        task = {
            'action': action,
            'model': session.description,
            'token': token,
            'tenant_id': environment.tenant_id,
            'id': environment.id
        }

        if not deleted:
            task['model']['Objects']['?']['id'] = environment.id
            task['model']['Objects']['applications'] = \
                task['model']['Objects'].get('services', [])

            if 'services' in task['model']['Objects']:
                del task['model']['Objects']['services']

        session.state = SessionState.DELETING if deleted \
            else SessionState.DEPLOYING
        deployment = models.Deployment()
        deployment.environment_id = session.environment_id
        deployment.description = token_sanitizer.TokenSanitizer().sanitize(
            session.description.get('Objects'))
        status = models.Status()
        status.text = ('Delete' if deleted else 'Deployment') + ' scheduled'
        status.level = 'info'
        deployment.statuses.append(status)

        with unit.begin():
            unit.add(session)
            unit.add(deployment)

        rpc.engine().handle_task(task)
