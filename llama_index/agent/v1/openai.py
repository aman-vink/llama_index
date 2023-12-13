import asyncio
import json
import logging
import uuid
from threading import Thread
from typing import Any, Dict, List, Optional, Tuple, Union, cast, get_args

from llama_index.agent.v1.schema import (
    BaseAgentStepEngine,
    Task,
    TaskStep,
    TaskStepOutput,
)
from llama_index.callbacks import (
    CallbackManager,
    CBEventType,
    EventPayload,
)
from llama_index.chat_engine.types import (
    AGENT_CHAT_RESPONSE_TYPE,
    AgentChatResponse,
    ChatResponseMode,
    StreamingAgentChatResponse,
)
from llama_index.llms.base import LLM, ChatMessage, ChatResponse, MessageRole
from llama_index.llms.openai import OpenAI
from llama_index.llms.openai_utils import OpenAIToolCall
from llama_index.memory.types import BaseMemory
from llama_index.objects.base import ObjectRetriever
from llama_index.tools import BaseTool, ToolOutput, adapt_to_async_tool

logger = logging.getLogger(__name__)
logger.setLevel(logging.WARNING)

DEFAULT_MAX_FUNCTION_CALLS = 5
DEFAULT_MODEL_NAME = "gpt-3.5-turbo-0613"


def get_function_by_name(tools: List[BaseTool], name: str) -> BaseTool:
    """Get function by name."""
    name_to_tool = {tool.metadata.name: tool for tool in tools}
    if name not in name_to_tool:
        raise ValueError(f"Tool with name {name} not found")
    return name_to_tool[name]


def call_tool_with_error_handling(
    tool: BaseTool,
    input_dict: Dict,
    error_message: Optional[str] = None,
    raise_error: bool = False,
) -> ToolOutput:
    """Call tool with error handling.

    Input is a dictionary with args and kwargs

    """
    try:
        return tool(**input_dict)
    except Exception as e:
        if raise_error:
            raise
        error_message = error_message or f"Error: {e!s}"
        return ToolOutput(
            content=error_message,
            tool_name=tool.metadata.name,
            raw_input={"kwargs": input_dict},
            raw_output=e,
        )


def call_function(
    tools: List[BaseTool],
    tool_call: OpenAIToolCall,
    verbose: bool = False,
) -> Tuple[ChatMessage, ToolOutput]:
    """Call a function and return the output as a string."""
    # validations to get passed mypy
    assert tool_call.id is not None
    assert tool_call.function is not None
    assert tool_call.function.name is not None
    assert tool_call.function.arguments is not None

    id_ = tool_call.id
    function_call = tool_call.function
    name = tool_call.function.name
    arguments_str = tool_call.function.arguments
    if verbose:
        print("=== Calling Function ===")
        print(f"Calling function: {name} with args: {arguments_str}")
    tool = get_function_by_name(tools, name)
    argument_dict = json.loads(arguments_str)

    # Call tool
    # Use default error message
    output = call_tool_with_error_handling(tool, argument_dict, error_message=None)
    if verbose:
        print(f"Got output: {output!s}")
        print("========================\n")
    return (
        ChatMessage(
            content=str(output),
            role=MessageRole.TOOL,
            additional_kwargs={
                "name": name,
                "tool_call_id": id_,
            },
        ),
        output,
    )


async def acall_function(
    tools: List[BaseTool], tool_call: OpenAIToolCall, verbose: bool = False
) -> Tuple[ChatMessage, ToolOutput]:
    """Call a function and return the output as a string."""
    # validations to get passed mypy
    assert tool_call.id is not None
    assert tool_call.function is not None
    assert tool_call.function.name is not None
    assert tool_call.function.arguments is not None

    id_ = tool_call.id
    function_call = tool_call.function
    name = tool_call.function.name
    arguments_str = tool_call.function.arguments
    if verbose:
        print("=== Calling Function ===")
        print(f"Calling function: {name} with args: {arguments_str}")
    tool = get_function_by_name(tools, name)
    async_tool = adapt_to_async_tool(tool)
    argument_dict = json.loads(arguments_str)
    output = await async_tool.acall(**argument_dict)
    if verbose:
        print(f"Got output: {output!s}")
        print("========================\n")
    return (
        ChatMessage(
            content=str(output),
            role=MessageRole.TOOL,
            additional_kwargs={
                "name": name,
                "tool_call_id": id_,
            },
        ),
        output,
    )


def resolve_tool_choice(tool_choice: Union[str, dict] = "auto") -> Union[str, dict]:
    """Resolve tool choice.

    If tool_choice is a function name string, return the appropriate dict.
    """
    if isinstance(tool_choice, str) and tool_choice not in ["none", "auto"]:
        return {"type": "function", "function": {"name": tool_choice}}

    return tool_choice


class OpenAIAgentStepEngine(BaseAgentStepEngine):
    """OpenAI Agent step engine."""

    def __init__(
        self,
        tools: List[BaseTool],
        llm: OpenAI,
        prefix_messages: List[ChatMessage],
        verbose: bool,
        max_function_calls: int = DEFAULT_MAX_FUNCTION_CALLS,
        callback_manager: Optional[CallbackManager] = None,
        tool_retriever: Optional[ObjectRetriever[BaseTool]] = None,
    ):
        self._llm = llm
        self._verbose = verbose
        self._max_function_calls = max_function_calls
        self.prefix_messages = prefix_messages
        self.callback_manager = callback_manager or self._llm.callback_manager

        if len(tools) > 0 and tool_retriever is not None:
            raise ValueError("Cannot specify both tools and tool_retriever")
        elif len(tools) > 0:
            self._get_tools = lambda _: tools
        elif tool_retriever is not None:
            tool_retriever_c = cast(ObjectRetriever[BaseTool], tool_retriever)
            self._get_tools = lambda message: tool_retriever_c.retrieve(message)
        else:
            # no tools
            self._get_tools = lambda _: []

    @classmethod
    def from_tools(
        cls,
        tools: Optional[List[BaseTool]] = None,
        tool_retriever: Optional[ObjectRetriever[BaseTool]] = None,
        llm: Optional[LLM] = None,
        verbose: bool = False,
        max_function_calls: int = DEFAULT_MAX_FUNCTION_CALLS,
        callback_manager: Optional[CallbackManager] = None,
        system_prompt: Optional[str] = None,
        prefix_messages: Optional[List[ChatMessage]] = None,
        **kwargs: Any,
    ) -> "OpenAIAgentStepEngine":
        """Create an OpenAIAgent from a list of tools.

        Similar to `from_defaults` in other classes, this method will
        infer defaults for a variety of parameters, including the LLM,
        if they are not specified.

        """
        tools = tools or []

        llm = llm or OpenAI(model=DEFAULT_MODEL_NAME)
        if not isinstance(llm, OpenAI):
            raise ValueError("llm must be a OpenAI instance")

        if callback_manager is not None:
            llm.callback_manager = callback_manager

        if not llm.metadata.is_function_calling_model:
            raise ValueError(
                f"Model name {llm.model} does not support function calling API. "
            )

        if system_prompt is not None:
            if prefix_messages is not None:
                raise ValueError(
                    "Cannot specify both system_prompt and prefix_messages"
                )
            prefix_messages = [ChatMessage(content=system_prompt, role="system")]

        prefix_messages = prefix_messages or []

        return cls(
            tools=tools,
            tool_retriever=tool_retriever,
            llm=llm,
            prefix_messages=prefix_messages,
            verbose=verbose,
            max_function_calls=max_function_calls,
            callback_manager=callback_manager,
        )

    def get_all_messages(self, step: TaskStep) -> List[ChatMessage]:
        return self.prefix_messages + step.memory.get()

    def get_latest_tool_calls(self, step: TaskStep) -> Optional[List[OpenAIToolCall]]:
        return step.memory.get_all()[-1].additional_kwargs.get("tool_calls", None)

    def _get_llm_chat_kwargs(
        self,
        step: TaskStep,
        openai_tools: List[dict],
        tool_choice: Union[str, dict] = "auto",
    ) -> Dict[str, Any]:
        llm_chat_kwargs: dict = {"messages": self.get_all_messages(step)}
        if openai_tools:
            llm_chat_kwargs.update(
                tools=openai_tools, tool_choice=resolve_tool_choice(tool_choice)
            )
        return llm_chat_kwargs

    def _process_message(
        self, step: TaskStep, chat_response: ChatResponse
    ) -> AgentChatResponse:
        ai_message = chat_response.message
        step.memory.put(ai_message)
        return AgentChatResponse(
            response=str(ai_message.content), sources=step.step_state["sources"]
        )

    def _get_stream_ai_response(
        self, step: TaskStep, **llm_chat_kwargs: Any
    ) -> StreamingAgentChatResponse:
        chat_stream_response = StreamingAgentChatResponse(
            chat_stream=self._llm.stream_chat(**llm_chat_kwargs),
            sources=self.sources,
        )
        # Get the response in a separate thread so we can yield the response
        thread = Thread(
            target=chat_stream_response.write_response_to_history,
            args=(step.memory,),
        )
        thread.start()
        # Wait for the event to be set
        chat_stream_response._is_function_not_none_thread_event.wait()
        # If it is executing an openAI function, wait for the thread to finish
        if chat_stream_response._is_function:
            thread.join()
        # if it's false, return the answer (to stream)
        return chat_stream_response

    async def _get_async_stream_ai_response(
        self, step: TaskStep, **llm_chat_kwargs: Any
    ) -> StreamingAgentChatResponse:
        chat_stream_response = StreamingAgentChatResponse(
            achat_stream=await self._llm.astream_chat(**llm_chat_kwargs),
            sources=self.sources,
        )
        # create task to write chat response to history
        asyncio.create_task(
            chat_stream_response.awrite_response_to_history(step.memory)
        )
        # wait until openAI functions stop executing
        await chat_stream_response._is_function_false_event.wait()
        # return response stream
        return chat_stream_response

    def _get_agent_response(
        self, step: TaskStep, mode: ChatResponseMode, **llm_chat_kwargs: Any
    ) -> AGENT_CHAT_RESPONSE_TYPE:
        if mode == ChatResponseMode.WAIT:
            chat_response: ChatResponse = self._llm.chat(**llm_chat_kwargs)
            return self._process_message(step, chat_response)
        elif mode == ChatResponseMode.STREAM:
            return self._get_stream_ai_response(step, **llm_chat_kwargs)
        else:
            raise NotImplementedError

    async def _get_async_agent_response(
        self, step: TaskStep, mode: ChatResponseMode, **llm_chat_kwargs: Any
    ) -> AGENT_CHAT_RESPONSE_TYPE:
        if mode == ChatResponseMode.WAIT:
            chat_response: ChatResponse = await self._llm.achat(**llm_chat_kwargs)
            return self._process_message(step, chat_response)
        elif mode == ChatResponseMode.STREAM:
            return await self._get_async_stream_ai_response(step, **llm_chat_kwargs)
        else:
            raise NotImplementedError

    def _call_function(
        self,
        tools: List[BaseTool],
        tool_call: OpenAIToolCall,
        memory: BaseMemory,
        sources: List[ToolOutput],
    ) -> None:
        function_call = tool_call.function
        # validations to get passed mypy
        assert function_call is not None
        assert function_call.name is not None
        assert function_call.arguments is not None

        with self.callback_manager.event(
            CBEventType.FUNCTION_CALL,
            payload={
                EventPayload.FUNCTION_CALL: function_call.arguments,
                EventPayload.TOOL: get_function_by_name(
                    tools, function_call.name
                ).metadata,
            },
        ) as event:
            function_message, tool_output = call_function(
                tools, tool_call, verbose=self._verbose
            )
            event.on_end(payload={EventPayload.FUNCTION_OUTPUT: str(tool_output)})
        sources.append(tool_output)
        memory.put(function_message)

    async def _acall_function(
        self,
        tools: List[BaseTool],
        tool_call: OpenAIToolCall,
        memory: BaseMemory,
        sources: List[ToolOutput],
    ) -> None:
        function_call = tool_call.function
        # validations to get passed mypy
        assert function_call is not None
        assert function_call.name is not None
        assert function_call.arguments is not None

        with self.callback_manager.event(
            CBEventType.FUNCTION_CALL,
            payload={
                EventPayload.FUNCTION_CALL: function_call.arguments,
                EventPayload.TOOL: get_function_by_name(
                    tools, function_call.name
                ).metadata,
            },
        ) as event:
            function_message, tool_output = await acall_function(
                tools, tool_call, verbose=self._verbose
            )
            event.on_end(payload={EventPayload.FUNCTION_OUTPUT: str(tool_output)})
        sources.append(tool_output)
        memory.put(function_message)

    def initialize_step(self, task: Task, **kwargs: Any) -> TaskStep:
        """Initialize step from task."""
        sources: List[ToolOutput] = []
        # initialize state in this step
        step_state = {
            "sources": sources,
            "n_function_calls": 0,
        }

        return TaskStep(
            task_id=task.task_id,
            step_id=str(uuid.uuid4()),
            input=task.input,
            memory=task.memory,
            step_state=step_state,
        )

    def _should_continue(
        self, tool_calls: Optional[List[OpenAIToolCall]], n_function_calls: int
    ) -> bool:
        if n_function_calls > self._max_function_calls:
            return False
        if not tool_calls:
            return False
        return True

    def get_tools(self, input: str) -> List[BaseTool]:
        """Get tools."""
        return self._get_tools(input)

    def _run_step(
        self,
        step: TaskStep,
        task: Task,
        mode: ChatResponseMode = ChatResponseMode.WAIT,
        tool_choice: Union[str, dict] = "auto",
    ) -> TaskStepOutput:
        """Run step."""
        # TODO: see if we want to do step-based inputs
        tools = self.get_tools(task.input)
        openai_tools = [tool.metadata.to_openai_tool() for tool in tools]

        llm_chat_kwargs = self._get_llm_chat_kwargs(step, openai_tools, tool_choice)
        agent_chat_response = self._get_agent_response(
            step, mode=mode, **llm_chat_kwargs
        )

        # TODO: implement _should_continue
        if not self._should_continue(
            self.get_latest_tool_calls(step), step.step_state["n_function_calls"]
        ):
            is_done = True
            # TODO: return response
        else:
            is_done = False
            for tool_call in self.get_latest_tool_calls(step):
                # Some validation
                if not isinstance(tool_call, get_args(OpenAIToolCall)):
                    raise ValueError("Invalid tool_call object")

                if tool_call.type != "function":
                    raise ValueError("Invalid tool type. Unsupported by OpenAI")
                # TODO: maybe execute this with multi-threading
                self._call_function(
                    tools, tool_call, step.memory, step.step_state["sources"]
                )
                # change function call to the default value, if a custom function was given
                # as an argument (none and auto are predefined by OpenAI)
                if tool_choice not in ("auto", "none"):
                    tool_choice = "auto"
                step.step_state["n_function_calls"] += 1

        # generate next step, append to task queue
        new_steps = (
            [
                TaskStep(
                    task_id=step.task_id,
                    step_id=str(uuid.uuid4()),
                    input=agent_chat_response,
                    memory=step.memory,
                )
            ]
            if not is_done
            else []
        )

        return TaskStepOutput(
            output=agent_chat_response,
            task_step=step,
            is_last=is_done,
            next_steps=new_steps,
        )

    async def _arun_step(
        self,
        step: TaskStep,
        task: Task,
        mode: ChatResponseMode = ChatResponseMode.WAIT,
        tool_choice: Union[str, dict] = "auto",
    ) -> TaskStepOutput:
        """Run step."""
        # TODO: see if we want to do step-based inputs
        tools = self.get_tools(task.input)
        openai_tools = [tool.metadata.to_openai_tool() for tool in tools]

        llm_chat_kwargs = self._get_llm_chat_kwargs(step, openai_tools, tool_choice)
        agent_chat_response = await self._get_async_agent_response(
            step, mode=mode, **llm_chat_kwargs
        )

        # TODO: implement _should_continue
        if not self._should_continue(
            self.get_latest_tool_calls(step), step.step_state["n_function_calls"]
        ):
            is_done = True
            # TODO: return response
        else:
            is_done = False
            for tool_call in self.get_latest_tool_calls(step):
                # Some validation
                if not isinstance(tool_call, get_args(OpenAIToolCall)):
                    raise ValueError("Invalid tool_call object")

                if tool_call.type != "function":
                    raise ValueError("Invalid tool type. Unsupported by OpenAI")
                # TODO: maybe execute this with multi-threading
                await self._acall_function(
                    tools, tool_call, step.memory, step.step_state["sources"]
                )
                # change function call to the default value, if a custom function was given
                # as an argument (none and auto are predefined by OpenAI)
                if tool_choice not in ("auto", "none"):
                    tool_choice = "auto"
                step.step_state["n_function_calls"] += 1

        # generate next step, append to task queue
        new_steps = (
            [
                TaskStep(
                    task_id=step.task_id,
                    step_id=str(uuid.uuid4()),
                    input=agent_chat_response,
                    memory=step.memory,
                )
            ]
            if not is_done
            else []
        )

        return TaskStepOutput(
            output=agent_chat_response,
            task_step=step,
            is_last=is_done,
            next_steps=new_steps,
        )

    def run_step(self, step: TaskStep, task: Task, **kwargs: Any) -> TaskStepOutput:
        """Run step."""
        tool_choice = kwargs.get("tool_choice", "auto")
        return self._run_step(
            step, task, mode=ChatResponseMode.WAIT, tool_choice=tool_choice
        )

    async def arun_step(
        self, step: TaskStep, task: Task, **kwargs: Any
    ) -> TaskStepOutput:
        """Run step (async)."""
        tool_choice = kwargs.get("tool_choice", "auto")
        return await self._arun_step(
            step, task, mode=ChatResponseMode.WAIT, tool_choice=tool_choice
        )

    def stream_step(self, step: TaskStep, task: Task, **kwargs: Any) -> TaskStepOutput:
        """Run step (stream)."""
        # TODO: figure out if we need a different type for TaskStepOutput
        raise NotImplementedError

    async def astream_step(
        self, step: TaskStep, task: Task, **kwargs: Any
    ) -> TaskStepOutput:
        """Run step (async stream)."""
        raise NotImplementedError
