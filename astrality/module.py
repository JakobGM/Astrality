"""Module implementing user configured custom functionality."""

from collections import namedtuple
from datetime import timedelta
import logging
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

from jinja2.exceptions import TemplateNotFound

from astrality import compiler
from astrality.compiler import context
from astrality.config import ApplicationConfig, insert_into, user_configuration
from astrality.filewatcher import DirectoryWatcher
from astrality.event_listener import EventListener, event_listener_factory
from astrality.utils import run_shell


ModuleConfig = Dict[str, Any]

ContextSectionImport = namedtuple(
    'ContextSectionImport',
    ['into_section', 'from_section', 'from_config_file'],
)
Template = namedtuple(
    'Template',
    ['source', 'target'],
)
WatchedFile = namedtuple(
    'WatchedFile',
    ['path', 'module', 'specified_path'],
)

logger = logging.getLogger('astrality')


class Module:
    """
    Class for determining module actions.

    A Module instance should only return actions to be performed, but never
    perform them. That is the responsibility of a ModuleManager instance.

    A module can define a set of commands to be run on astrality startup, and
    exit, in addition to every time a given type of event changes.

    Commands are run in the users shell, and can use the following placeholder:
    - {event}: The event specified by the event_listener instance.
    """

    def __init__(self, module_config: ModuleConfig) -> None:
        """
        Initialize Module object with a section from a config dictionary.

        Section name must be [module/*], where * is the module name.
        In addition, the enabled option must be set to "true", or not set at
        all.

        module_config example:
        {'module/name':
            'enabled': True,
            'event_listener': {'type': 'weekday'},
            'on_startup': {'run': ['echo weekday is {event}']},
        }
        """
        # Can only initialize one module at a time
        assert len(module_config) == 1

        section = next(iter(module_config.keys()))
        self.name: str = section.split('/')[1]

        self.module_config = module_config[section]
        self.populate_event_blocks()

        # Import trigger actions into their respective event blocks
        self.import_trigger_actions()

        # Use static event_listener if no event_listener is specified
        self.event_listener: EventListener = event_listener_factory(
            self.module_config.get('event_listener', {'type': 'static'}),
        )

    def populate_event_blocks(self) -> None:
        """
        Populate non-configured actions within event blocks.

        This prevents us from having to use .get() all over the Module.
        """
        for event_block in ('on_startup', 'on_event', 'on_exit', ):
            configured_event_block = self.module_config.get(event_block, {})
            self.module_config[event_block] = {
                'import_context': [],
                'compile': [],
                'run': [],
                'trigger': [],
            }
            self.module_config[event_block].update(configured_event_block)

        if not 'on_modified' in self.module_config:
            self.module_config['on_modified'] = {}
        else:
            for template_name in self.module_config['on_modified'].keys():
                configured_event_block = self.module_config['on_modified'][template_name]
                self.module_config['on_modified'][template_name] = {
                    'import_context': [],
                    'compile': [],
                    'run': [],
                    'trigger': [],
                }
                self.module_config['on_modified'][template_name].update(configured_event_block)

        # Convert any single actions into a list of that action, allowing
        # users to not use lists in their configuration if they only have one
        # action
        for event_block in (
            self.module_config['on_startup'],
            self.module_config['on_event'],
            self.module_config['on_exit'],
            *self.module_config['on_modified'].values(),
        ):
            for action in ('import_context', 'compile', 'run', 'trigger',):
                if not isinstance(event_block[action], list):  # type: ignore
                    event_block[action] = [event_block[action]]  # type: ignore

    def import_trigger_actions(self) -> None:
        """If an event block defines trigger events, import those actions."""
        event_blocks = (
            self.module_config['on_startup'],
            self.module_config['on_event'],
            self.module_config['on_exit'],
            self.module_config['on_modified'].values(),
        )
        for event_block in event_blocks:
            if 'trigger' in event_block:
                event_blocks_to_import = event_block['trigger']
                if isinstance(event_blocks_to_import, str):
                    event_blocks_to_import = [event_blocks_to_import]

                for event_block_to_import in event_blocks_to_import:
                    self._import_event_block(
                        from_event_block=event_block_to_import,
                        into=event_block,
                    )

    def _import_event_block(
        self,
        from_event_block: str,
        into: Dict[str, Any],
    ) -> None:
        """Merge one event block with another one."""
        if 'on_modified.' in from_event_block:
            template = from_event_block[12:]
            from_event_block_dict = self.module_config['on_modified'].get(template, {})
        else:
            from_event_block_dict = self.module_config[from_event_block]

        into['run'].extend(from_event_block_dict['run'])
        into['import_context'].extend(from_event_block_dict['import_context'])
        into['compile'].extend(from_event_block_dict['compile'])

    def startup_commands(self) -> Tuple[str, ...]:
        """Return commands to be run on Module instance startup."""
        startup_commands: List[str] = self.module_config['on_startup']['run']

        if len(startup_commands) == 0:
            logger.debug(f'[module/{self.name}] No startup command specified.')
            return ()
        else:
            return tuple(
                self.interpolate_string(command)
                for command
                in startup_commands
            )

    def on_event_commands(self) -> Tuple[str, ...]:
        """Commands to be run when self.event_listener event changes."""
        on_event_commands: List[str] = self.module_config['on_event']['run']

        if len(on_event_commands) == 0:
            logger.debug(f'[module/{self.name}] No event command specified.')
            return ()
        else:
            return tuple(
                self.interpolate_string(command)
                for command
                in on_event_commands
            )

    def exit_commands(self) -> Tuple[str, ...]:
        """Commands to be run on Module instance shutdown."""
        exit_commands: List[str] = self.module_config['on_exit']['run']

        if len(exit_commands) == 0:
            logger.debug(f'[module/{self.name}] No exit command specified.')
            return ()
        else:
            return tuple(
                self.interpolate_string(command)
                for command
                in exit_commands
            )

    def modified_commands(self, template_name: str) -> Tuple[str, ...]:
        """Commands to be run when a module template is modified."""
        modified_commands: List[str] = self.module_config['on_modified'][template_name]['run']

        if len(modified_commands) == 0:
            logger.debug(f'[module/{self.name}] No modified command specified.')
            return ()
        else:
            return tuple(
                self.interpolate_string(command)
                for command
                in modified_commands
            )


    def context_section_imports(
        self,
        trigger: str,
    ) -> Tuple[ContextSectionImport, ...]:
        """
        Return what to import into the global application_context.

        Trigger is one of 'on_startup', 'on_event', or 'on_exit'.
        This determines which section of the module is used to get the context
        import specification from.
        """
        assert trigger in ('on_startup', 'on_event', 'on_startup',)

        context_section_imports = []
        import_config = self.module_config.get(
            trigger,
            {},
        ).get('import_context', [])

        for context_import in import_config:
            # Insert placeholders
            from_path = self.interpolate_string(context_import['from_path'])

            from_section: Optional[str]
            to_section: Optional[str]

            if 'from_section' in context_import:
                from_section = self.interpolate_string(context_import['from_section'])

                # If no `to_section` is specified, use the same section as
                # `from_section`
                to_section = self.interpolate_string(
                    context_import.get('to_section', from_section),
                )
            else:
                # From section is not specified, so set both to None, indicating
                # a wish to import *all* sections
                from_section = None
                to_section = None

            # Get the absolute path
            config_path = Path(from_path)

            # Isert a ContextSectionImport tuple into the return value
            context_section_imports.append(
                ContextSectionImport(
                    into_section=to_section,
                    from_section=from_section,
                    from_config_file=config_path,
                )
            )

        return tuple(context_section_imports)


    def interpolate_string(self, string: str) -> str:
        """
        Replace all module placeholders in string.

        For now, the module only replaces {event} placeholders.
        """
        return string.replace('{event}', self.event_listener.event())

    @staticmethod
    def valid_class_section(
        section: ModuleConfig,
        requires_timeout: Union[int, float],
        requires_working_directory: Path,
    ) -> bool:
        """Check if the given dict represents a valid enabled module."""

        if not len(section) == 1:
            raise RuntimeError(
                'Tried to check module section with dict '
                'which does not have exactly one item.',
            )

        try:
            module_name = next(iter(section.keys()))
            valid_module_name = module_name.split('/')[0] == 'module'  # type: ignore
            enabled = section[module_name].get('enabled', True)
            if not (valid_module_name and enabled):
                return False

        except KeyError:
            return False

        # The module is enabled, now check if all requirements are satisfied
        requires = section[module_name].get('requires')
        if not requires:
            return True
        else:
            if isinstance(requires, str):
                requires = [requires]

            for requirement in requires:
                if run_shell(command=requirement, fallback=False) is False:
                    logger.warning(
                        f'[{module_name}] Module does not satisfy requirement "{requirement}".',
                    )
                    return False

            logger.info(
                f'[{module_name}] Module satisfies all requirements.'
            )
            return True

class ModuleManager:
    """A manager for operating on a set of modules."""

    def __init__(self, config: ApplicationConfig) -> None:
        self.application_config = config
        self.config_directory = Path(config['_runtime']['config_directory'])
        self.temp_directory = Path(config['_runtime']['temp_directory'])

        self.application_context = context(config)
        self.modules: Dict[str, Module] = {}
        self.startup_done = False
        self.last_module_events: Dict[str, str] = {}

        for section, options in config.items():
            module_config = {section: options}
            if Module.valid_class_section(
                section=module_config,
                requires_timeout=self.application_config['settings/astrality']['requires_timeout'],
                requires_working_directory=self.application_config['_runtime']['config_directory'],
            ):
                module = Module(module_config=module_config)
                self.modules[module.name] = module

        self.templates = self.prepare_templates(self.modules.values())
        self.string_replacements = self.generate_string_replacements(self.templates)
        self.on_modified_paths = self.find_on_modified_paths(self.modules.values())

        # Initialize the config directory watcher, but don't start it yet
        self.directory_watcher = DirectoryWatcher(
            directory=self.application_config['_runtime']['config_directory'],
            on_modified=self.modified,
        )

    def __len__(self) -> int:
        """Return the number of managed modules."""
        return len(self.modules)

    def prepare_templates(
        self,
        modules: Iterable[Module],
    ) -> Dict[str, Template]:
        """
        Prepare the use of templates that could be compiled by `modules`.

        Returns a dictionary where the user provided path to the template is
        the key, and the value is a Template NamedTuple instance, containing
        the source and target Paths of the template.
        """
        templates: Dict[str, Template] = {}

        for module in modules:
            # All the module blocks which can contain compile actions
            for block in (
                    module.module_config['on_startup'],
                    module.module_config['on_event'],
                    module.module_config['on_exit'],
                    *module.module_config['on_modified'].values(),
            ):
                for compile_action in block['compile']:
                    specified_source = compile_action['template']
                    absolute_source = self.expand_path(Path(specified_source))

                    if 'target' in compile_action:
                        target = self.expand_path(Path(compile_action['target']))
                    else:
                        target = self.create_temp_file(name=module.name)

                    templates[specified_source] = Template(
                        source=absolute_source,
                        target=target,
                    )

        return templates


    def find_on_modified_paths(
        self,
        modules: Iterable[Module],
    ) -> Dict[Path, WatchedFile]:
        """
        Return a dictionary keyed to all modification watched file paths.

        Return a dictionary with the paths to watched files as the keys and
        WatchedFile instances as values.
        """
        on_modified_paths: Dict[Path, WatchedFile] = {}

        for module in modules:
            for watched_for_modification in module.module_config['on_modified'].keys():
                on_modified_path = self.expand_path(
                    Path(watched_for_modification),
                )
                on_modified_paths[on_modified_path] = WatchedFile(
                    path=on_modified_path,
                    module=module,
                    specified_path=watched_for_modification,
                )

        return on_modified_paths

    def generate_string_replacements(
        self,
        templates: Dict[str, Template],
    ) -> Dict[str, str]:
        """
        Returns a dictionary containing all string replacements keyed to their
        placeholders.

        Includes template path placeholders replaced with the compilation
        target path.
        """
        string_replacements: Dict[str, str] = {}

        for specified_path, template in templates.items():
            string_replacements[specified_path] = str(template.target)

        return string_replacements

    def module_events(self) -> Dict[str, str]:
        """Return dict containing the event of all modules."""
        module_events = {}
        for module_name, module in self.modules.items():
            module_events[module_name] = module.event_listener.event()

        return module_events

    def finish_tasks(self) -> None:
        """
        Finish all due tasks defined by the managed modules.

        The order of finishing tasks is as follows:
            1) Import any relevant context sections.
            2) Compile all templates with the new section.
            3) Run startup commands, if it is not already done.
            4) Run on_event commands, if it is not already done for this
               module events combination.
        """
        if not self.startup_done:
            # Save the last event configuration, such that on_event
            # is only run when the event *changes*
            self.last_module_events = self.module_events()

            # Perform all startup actions
            self.import_context_sections('on_startup')
            self.compile_templates('on_startup')
            self.startup()
        elif self.last_module_events != self.module_events():
            self.import_context_sections('on_event')
            self.compile_templates('on_event')
            self.on_event()

    def has_unfinished_tasks(self) -> bool:
        """Return True if there are any module tasks due."""
        if not self.startup_done:
            return True
        else:
            return self.last_module_events != self.module_events()

    def time_until_next_event(self) -> timedelta:
        """Time left until first event change of any of the modules managed."""
        return min(
            module.event_listener.time_until_next_event()
            for module
            in self.modules.values()
        )

    def import_context_sections(self, trigger: str) -> None:
        """
        Import context sections defined by the managed modules.

        Trigger is one of 'on_startup', 'on_event', or 'on_exit'.
        This determines which event block of the module is used to get the
        context import specification from.
        """
        assert trigger in ('on_startup', 'on_event', 'on_exit',)

        for module in self.modules.values():
            context_section_imports = module.context_section_imports(trigger)

            for csi in context_section_imports:
                self.application_context = insert_into(
                    context=self.application_context,
                    section=csi.into_section,
                    from_section=csi.from_section,
                    from_config_file=self.expand_path(csi.from_config_file),
                )

    def compile_templates(self, trigger: str) -> None:
        """
        Compile the module templates specified by the `templates` option.

        Trigger is one of 'on_startup', 'on_event', or 'on_exit'.
        This determines which section of the module is used to get the compile
        specification from.
        """
        assert trigger in ('on_startup', 'on_event', 'on_exit',)

        for module in self.modules.values():
            for compilation in module.module_config[trigger]['compile']:
                specified_path = compilation['template']
                template = self.templates[specified_path]
                self.compile_template(
                    source=template.source,
                    target=template.target,
                )


    def compile_template(self, source: Path, target: Path) -> None:
        """
        Compile a single template given by its shortname.

        A shortname is given either by shortname, implying that the module given
        defines that template, or by module_name.shortname, making it explicit.
        """
        try:
            compiler.compile_template(
                template=source,
                target=target,
                context=self.application_context,
                shell_command_working_directory=self.config_directory,
            )
        except TemplateNotFound:
            logger.error(
                f'Could not compile template "{source}" to target "{target}". '
                'Template does not exist.'
            )

    def startup(self):
        """Run all startup commands specified by the managed modules."""
        for module in self.modules.values():
            startup_commands = module.startup_commands()
            for command in startup_commands:
                logger.info(f'[module/{module.name}] Running startup command.')
                self.run_shell(
                    command=command,
                    timeout=self.application_config['settings/astrality']['run_timeout'],
                    module_name=module.name,
                )

        self.startup_done = True

        # Start watching config directory for file changes
        self.directory_watcher.start()

    def on_event(self):
        """Run all event change commands specified by the managed modules."""
        for module in self.modules.values():
            on_event_commands = module.on_event_commands()
            for command in on_event_commands:
                logger.info(f'[module/{module.name}] Running event command.')
                self.run_shell(
                    command=command,
                    timeout=self.application_config['settings/astrality']['run_timeout'],
                    module_name=module.name,
                )

        self.last_module_events = self.module_events()

    def exit(self):
        """
        Run all exit commands specified by the managed modules.

        Also close all temporary file handlers created by the modules.
        """
        for module in self.modules.values():
            exit_commands = module.exit_commands()
            for command in exit_commands:
                logger.info(f'[module/{module.name}] Running exit command.')
                self.run_shell(
                    command=command,
                    timeout=self.application_config['settings/astrality']['run_timeout'],
                    module_name=module.name,
                )

        if hasattr(self, 'temp_files'):
            for temp_file in self.temp_files:
                temp_file.close()

        # Stop watching config directory for file changes
        self.directory_watcher.stop()

    def modified(self, modified: Path):
        """
        Callback for when files within the config directory are modified.

        Run any context imports, compilations, and shell commands specified
        within the on_modified event block of each module.

        Also, if hot_reload is True, we reinstantiate the ModuleManager object
        if the application configuration has been modified.
        """
        if modified == self.application_config['_runtime']['config_directory'] / 'astrality.yaml':
            # The application configuration file has been modified

            if not self.application_config['settings/astrality']['hot_reload']:
                # Hot reloading is not enabled, so we return early
                return

            # Hot reloading is enabled, get the new configuration dict
            new_application_config = user_configuration(
                config_directory=modified.parent,
            )

            try:
                # Reinstantiate this object
                new_module_manager = ModuleManager(new_application_config)

                # Run all old exit actions, since the new config is valid
                self.exit()

                # Swap place with the new configuration
                self = new_module_manager

                # Run startup commands from the new configuration
                self.finish_tasks()
            except:
                # New configuration is invalid, just keep the old one
                # TODO: Test this behaviour
                logger.error('New configuration detected, but it is invalid!')
                pass

            return

        if not modified in self.on_modified_paths:
            # The modified file is not specified in any of the modules
            return

        watched_file = self.on_modified_paths[modified]
        module = watched_file.module
        specified_path = watched_file.specified_path
        template = self.templates[specified_path]

        for compilation in module.module_config['on_modified'][specified_path]['compile']:
            template = self.templates[compilation['template']]
            self.compile_template(
                source=template.source,
                target=template.target,
            )

        modified_commands = module.modified_commands(specified_path)
        for command in modified_commands:
            logger.info(f'[module/{module.name}] Running modified command.')
            self.run_shell(
                command=command,
                timeout=self.application_config['settings/astrality']['run_timeout'],
                module_name=module.name,
            )

    def run_shell(
        self,
        command: str,
        timeout: Union[int, float],
        module_name: Optional[str] = None,
    ) -> None:
        """Run a shell command defined by a managed module."""

        command = self.interpolate_string(command)

        if module_name:
            logger.info(f'[module/{module_name}] Running command "{command}".')

        run_shell(
            command=command,
            timeout=timeout,
            working_directory=self.application_config['_runtime']['config_directory'],
        )

    def expand_path(self, path: Path) -> Path:
        """
        Return an absolute path from a (possibly) relative path.

        Relative paths are relative to $ASTRALITY_CONFIG_HOME, and ~ is
        expanded to the home directory of $USER.
        """

        path = Path.expanduser(path)

        if not path.is_absolute():
            path = Path(
                self.config_directory,
                path,
            )

        return path

    def interpolate_string(self, string: str) -> str:
        """Replace all template placeholders with the compilation path."""

        for specified_path, template in self.templates.items():
            string = string.replace(
                '{' + specified_path + '}',
                str(template.target),
            )
        return string

    def create_temp_file(self, name) -> Path:
        """Create a temp file used as a compilation target, returning its path."""

        temp_file = NamedTemporaryFile(  # type: ignore
            prefix=name + '-',
            # dir=Path(self.temp_directory),
        )

        # NB: These temporary files need to be persisted during the entirity of
        # the scripts runtime, since the files are deleted when they go out of
        # scope.
        if not hasattr(self, 'temp_files'):
            self.temp_files = [temp_file]
        else:
            self.temp_files.append(temp_file)

        return Path(temp_file.name)
