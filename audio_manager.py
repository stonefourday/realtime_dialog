import asyncio
import uuid
import queue
import threading
import time
import random
from typing import Optional, Dict, Any
import wave
import pyaudio
import signal
from dataclasses import dataclass

import config
from realtime_dialog_client import RealtimeDialogClient


@dataclass
class AudioConfig:
    """音频配置数据类"""
    format: str
    bit_size: int
    channels: int
    sample_rate: int
    chunk: int


class AudioDeviceManager:
    """音频设备管理类，处理音频输入输出"""

    def __init__(self, input_config: AudioConfig, output_config: AudioConfig):
        self.input_config = input_config
        self.output_config = output_config
        self.pyaudio = pyaudio.PyAudio()
        self.input_stream: Optional[pyaudio.Stream] = None
        self.output_stream: Optional[pyaudio.Stream] = None

    def open_input_stream(self) -> pyaudio.Stream:
        """打开音频输入流"""
        # p = pyaudio.PyAudio()
        self.input_stream = self.pyaudio.open(
            format=self.input_config.bit_size,
            channels=self.input_config.channels,
            rate=self.input_config.sample_rate,
            input=True,
            frames_per_buffer=self.input_config.chunk
        )
        return self.input_stream

    def open_output_stream(self) -> pyaudio.Stream:
        """打开音频输出流"""
        self.output_stream = self.pyaudio.open(
            format=self.output_config.bit_size,
            channels=self.output_config.channels,
            rate=self.output_config.sample_rate,
            output=True,
            frames_per_buffer=self.output_config.chunk
        )
        return self.output_stream

    def cleanup(self) -> None:
        """清理音频设备资源"""
        for stream in [self.input_stream, self.output_stream]:
            if stream:
                stream.stop_stream()
                stream.close()
        self.pyaudio.terminate()


class DialogSession:
    """对话会话管理类"""

    def __init__(self, ws_config: Dict[str, Any]):
        self.session_id = str(uuid.uuid4())
        self.client = RealtimeDialogClient(config=ws_config, session_id=self.session_id)
        self.audio_device = AudioDeviceManager(
            AudioConfig(**config.input_audio_config),
            AudioConfig(**config.output_audio_config)
        )

        self.is_running = True
        self.is_session_finished = False
        self.is_user_querying = False
        self.is_sending_chat_tts_text = False
        self.audio_buffer = b''

        signal.signal(signal.SIGINT, self._keyboard_signal)
        # 初始化音频队列和输出流
        self.audio_queue = queue.Queue()
        self.output_stream = self.audio_device.open_output_stream()
        # 启动播放线程
        self.is_recording = True
        self.is_playing = True
        self.player_thread = threading.Thread(target=self._audio_player_thread)
        self.player_thread.daemon = True
        self.player_thread.start()

    def _audio_player_thread(self):
        """音频播放线程"""
        while self.is_playing:
            try:
                # 从队列获取音频数据
                audio_data = self.audio_queue.get(timeout=1.0)
                if audio_data is not None:
                    self.output_stream.write(audio_data)
            except queue.Empty:
                # 队列为空时等待一小段时间
                time.sleep(0.1)
            except Exception as e:
                print(f"音频播放错误: {e}")
                time.sleep(0.1)

    def handle_server_response(self, response: Dict[str, Any]) -> None:
        if response == {}:
            return
        """处理服务器响应"""
        if response['message_type'] == 'SERVER_ACK' and isinstance(response.get('payload_msg'), bytes):
            # print(f"\n接收到音频数据: {len(response['payload_msg'])} 字节")
            if self.is_sending_chat_tts_text:
                return
            audio_data = response['payload_msg']
            self.audio_queue.put(audio_data)
            self.audio_buffer += audio_data
        elif response['message_type'] == 'SERVER_FULL_RESPONSE':
            print(f"服务器响应: {response}")
            event = response.get('event')
            payload_msg = response.get('payload_msg', {})

            if event == 450:
                print(f"清空缓存音频: {response['session_id']}")
                while not self.audio_queue.empty():
                    try:
                        self.audio_queue.get_nowait()
                    except queue.Empty:
                        continue
                self.is_user_querying = True

            if event == 350 and self.is_sending_chat_tts_text and payload_msg.get("tts_type") == "chat_tts_text":
                while not self.audio_queue.empty():
                    try:
                        self.audio_queue.get_nowait()
                    except queue.Empty:
                        continue
                self.is_sending_chat_tts_text = False

            if event == 459:
                self.is_user_querying = False
                if random.randint(0, 1) == 0:
                    self.is_sending_chat_tts_text = True
                    asyncio.create_task(self.trigger_chat_tts_text())
        elif response['message_type'] == 'SERVER_ERROR':
            print(f"服务器错误: {response['payload_msg']}")
            raise Exception("服务器错误")

    async def trigger_chat_tts_text(self):
        """概率触发发送ChatTTSText请求"""
        print("hit ChatTTSText event, start sending...")
        await self.client.chat_tts_text(
            is_user_querying=self.is_user_querying,
            start=True,
            end=False,
            content="这是第一轮TTS的开始和中间包事件，这两个合而为一了。",
        )
        await self.client.chat_tts_text(
            is_user_querying=self.is_user_querying,
            start=False,
            end=True,
            content="这是第一轮TTS的结束事件。",
        )
        await asyncio.sleep(10)
        await self.client.chat_tts_text(
            is_user_querying=self.is_user_querying,
            start=True,
            end=False,
            content="这是第二轮TTS的开始和中间包事件，这两个合而为一了。",
        )
        await self.client.chat_tts_text(
            is_user_querying=self.is_user_querying,
            start=False,
            end=True,
            content="这是第二轮TTS的结束事件。",
        )

    def _keyboard_signal(self, sig, frame):
        print(f"receive keyboard Ctrl+C")
        self.is_recording = False
        self.is_playing = False
        self.is_running = False

    async def receive_loop(self):
        try:
            while True:
                response = await self.client.receive_server_response()
                self.handle_server_response(response)
                if 'event' in response and (response['event'] == 152 or response['event'] == 153):
                    print(f"receive session finished event: {response['event']}")
                    self.is_session_finished = True
                    break
        except asyncio.CancelledError:
            print("接收任务已取消")
        except Exception as e:
            print(f"接收消息错误: {e}")

    async def process_microphone_input(self) -> None:
        await self.client.say_hello()
        """处理麦克风输入"""
        stream = self.audio_device.open_input_stream()
        print("已打开麦克风，请讲话...")

        while self.is_recording:
            try:
                # 添加exception_on_overflow=False参数来忽略溢出错误
                audio_data = stream.read(config.input_audio_config["chunk"], exception_on_overflow=False)
                save_pcm_to_wav(audio_data, "input.pcm")
                await self.client.task_request(audio_data)
                await asyncio.sleep(0.01)  # 避免CPU过度使用
            except Exception as e:
                print(f"读取麦克风数据出错: {e}")
                await asyncio.sleep(0.1)  # 给系统一些恢复时间

    async def start(self) -> None:
        """启动对话会话"""
        try:
            await self.client.connect()
            asyncio.create_task(self.process_microphone_input())
            asyncio.create_task(self.receive_loop())

            while self.is_running:
                await asyncio.sleep(0.1)

            await self.client.finish_session()
            while not self.is_session_finished:
                await asyncio.sleep(0.1)
            await self.client.finish_connection()
            await asyncio.sleep(0.1)
            await self.client.close()
            print(f"dialog request logid: {self.client.logid}")
            save_audio_to_pcm_file(self.audio_buffer, "output.pcm")
        except Exception as e:
            print(f"会话错误: {e}")
        finally:
            self.audio_device.cleanup()


def save_pcm_to_wav(pcm_data: bytes, filename: str) -> None:
    """保存PCM数据为WAV文件"""
    with wave.open(filename, 'wb') as wf:
        wf.setnchannels(config.input_audio_config["channels"])
        wf.setsampwidth(2)  # paInt16 = 2 bytes
        wf.setframerate(config.input_audio_config["sample_rate"])
        wf.writeframes(pcm_data)


def save_audio_to_pcm_file(audio_data: bytes, filename: str) -> None:
    """保存原始PCM音频数据到文件"""
    if not audio_data:
        print("No audio data to save.")
        return
    try:
        with open(filename, 'wb') as f:
            f.write(audio_data)
    except IOError as e:
        print(f"Failed to save pcm file: {e}")
