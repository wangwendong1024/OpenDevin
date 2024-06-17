import base64
import pickle
from dataclasses import dataclass, field

from opendevin.controller.state.task import RootTask
from opendevin.core.logger import opendevin_logger as logger
from opendevin.core.metrics import Metrics
from opendevin.core.schema import AgentState
from opendevin.events.action import (
    MessageAction,
)
from opendevin.events.action.agent import (
    AgentDelegateSummaryAction,
    AgentFinishAction,
    AgentSummarizeAction,
)
from opendevin.events.observation import (
    CmdOutputObservation,
)
from opendevin.memory.history import ShortTermHistory
from opendevin.storage import get_file_store

RESUMABLE_STATES = [
    AgentState.RUNNING,
    AgentState.PAUSED,
    AgentState.AWAITING_USER_INPUT,
    AgentState.FINISHED,
]


@dataclass
class State:
    root_task: RootTask = field(default_factory=RootTask)
    iteration: int = 0
    max_iterations: int = 100
    # number of characters we have sent to and received from LLM so far for current task
    num_of_chars: int = 0
    background_commands_obs: list[CmdOutputObservation] = field(default_factory=list)
    history: ShortTermHistory = field(default_factory=ShortTermHistory)
    inputs: dict = field(default_factory=dict)
    outputs: dict = field(default_factory=dict)
    error: str | None = None
    agent_state: AgentState = AgentState.LOADING
    resume_state: AgentState | None = None
    metrics: Metrics = Metrics()
    # root agent has level 0, and every delegate increases the level by one
    delegate_level: int = 0
    start_id: int = -1
    end_id: int = -1
    summaries: dict[tuple[int, int], AgentSummarizeAction] = field(default_factory=dict)
    delegate_summaries: dict[tuple[int, int], AgentDelegateSummaryAction] = field(
        default_factory=dict
    )

    def save_to_session(self, sid: str):
        fs = get_file_store()
        pickled = pickle.dumps(self)
        logger.debug(f'Saving state to session {sid}:{self.agent_state}')
        encoded = base64.b64encode(pickled).decode('utf-8')
        try:
            fs.write(f'sessions/{sid}/agent_state.pkl', encoded)
        except Exception as e:
            logger.error(f'Failed to save state to session: {e}')
            raise e

    @staticmethod
    def restore_from_session(sid: str) -> 'State':
        fs = get_file_store()
        try:
            encoded = fs.read(f'sessions/{sid}/agent_state.pkl')
            pickled = base64.b64decode(encoded)
            state = pickle.loads(pickled)
        except Exception as e:
            logger.error(f'Failed to restore state from session: {e}')
            raise e
        if state.agent_state in RESUMABLE_STATES:
            state.resume_state = state.agent_state
        else:
            state.resume_state = None
        state.agent_state = AgentState.LOADING
        return state

    def __getstate__(self):
        state = self.__dict__.copy()

        # save the relevant data from recent history
        # so that we can restore it when the state is restored
        if 'history' in state:
            state['summaries'] = state['history'].summaries
            state['delegate_summaries'] = state['history'].delegate_summaries
            state['start_id'] = state['history'].start_id
            state['end_id'] = state['history'].end_id

        # don't save history object itself
        state.pop('history', None)
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)

        # recreate the history object
        if not hasattr(self, 'history'):
            self.history = ShortTermHistory()

        # restore the relevant data in history from the state
        if hasattr(self, 'summaries'):
            self.history.summaries = self.summaries
        if hasattr(self, 'delegate_summaries'):
            self.history.delegate_summaries = self.delegate_summaries
        self.history.start_id = self.start_id
        self.history.end_id = self.end_id

        # remove the restored data from the state
        self.summaries = {}
        self.delegate_summaries = {}

    def get_current_user_intent(self):
        """
        Returns the latest user message that appears after a FinishAction, or the first (the task) if nothing was finished yet.
        """
        last_user_message = None
        for event in self.history.get_events(reverse=True):
            if isinstance(event, MessageAction) and event.source == 'user':
                last_user_message = event.content
            elif isinstance(event, AgentFinishAction):
                if last_user_message is not None:
                    return last_user_message

        return last_user_message
