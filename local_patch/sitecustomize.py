"""Automatically load project-owned VideoCaptioner runtime adjustments."""

from videocaptioner_video_formats import install as install_video_formats

install_video_formats()

from videocaptioner_gui_config_bridge import install as install_gui_config_bridge
from videocaptioner_audio_conversion_fix import install as install_audio_conversion_fix
from videocaptioner_logging_fix import install as install_logging_fix
from videocaptioner_output_format_fix import install as install_output_format_fix
from videocaptioner_request_logger_fix import install as install_request_logger_fix
from videocaptioner_shared_secret import install as install_shared_secret
from videocaptioner_translation_resilience_fix import (
    install as install_translation_resilience_fix,
)

install_gui_config_bridge()
install_audio_conversion_fix()
install_logging_fix()
install_request_logger_fix()
install_translation_resilience_fix()
install_output_format_fix()
install_shared_secret()
