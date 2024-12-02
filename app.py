import os
import sys
import errno
import configparser
from flask import Flask, request, abort, send_from_directory
from PIL import Image
import io
import tempfile
import google.generativeai as genai
import typing_extensions as typing
import azure.cognitiveservices.speech as speechsdk
from azure.storage.blob import BlobServiceClient
# from azure.cognitiveservices.speech import SpeechConfig, SpeechSynthesizer, AudioDataStream, SpeechSynthesisOutputFormat
# from azure.cognitiveservices.speech.audio import AudioOutputConfig

from linebot.v3 import (
    WebhookHandler
)
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.webhooks import (
    MessageEvent,
    TextMessageContent,
    AudioMessageContent,
    ImageMessageContent,
)
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    MessagingApiBlob,
    ReplyMessageRequest,
    TextMessage,
    StickerMessage,
    AudioMessage
)

# Config Parser
config = configparser.ConfigParser()
config.read('config.ini')

# 公開的 Azure URL
# PUBLIC_URL = "https://order-assistant-20241202.azurewebsites.net"

# 初始化 Line Messaging API 客戶端
LINE_CHANNEL_ACCESS_TOKEN = config['Line']['CHANNEL_ACCESS_TOKEN']
LINE_CHANNEL_SECRET = config['Line']['CHANNEL_SECRET']
if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET:
    print("Channel Secret 或 Access Token 未設置")
    sys.exit(1)

# 初始化 Messaging API 客戶端
api_config = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)

# 設定檔案暫存資料夾
# static_tmp_path = os.path.join(os.path.dirname(__file__), 'static', 'tmp')

# 初始化 Webhook Handler
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# 設定 Google Generative AI
genai.configure(api_key=config['Google']['GEMINI_API_KEY'])
model = genai.GenerativeModel('gemini-1.5-flash')

# 設定 Azure Speech
speech_key = config['Azure']['AZURE_SPEECH_KEY']
speech_region = config['Azure']['AZURE_REGION']
speech_config = speechsdk.SpeechConfig(subscription=speech_key, region=speech_region)
speech_config.speech_synthesis_voice_name = "zh-CN-XiaoxiaoNeural" 

# 設定 Azure Blob Service
connection_string = os.getenv('AZURE_STORAGE_ACCOUNT_KEY')
# connection_string = config['Azure']['AZURE_STORAGE_CONNECTION_STRING']
BLOB_CONTAINER_NAME = "static-tmp"
blob_service_client = BlobServiceClient.from_connection_string(connection_string)
blob_container_client = blob_service_client.get_container_client(BLOB_CONTAINER_NAME)

# 初始化 Flask 應用程式
app = Flask(__name__)
# app.config["UPLOAD_FOLDER"] = "static/tmp"
# 設定檔案暫存資料夾
# app.config["UPLOAD_FOLDER"] = tempfile.gettempdir()

# 初始化全域變數來保存
detected_language = ""
language_isornot = None
function_type = ""

# 創建暫存檔資料夾
# def make_static_tmp_dir():
#     try:
#         os.makedirs(static_tmp_path)
#     except OSError as exc:
#         if exc.errno == errno.EEXIST and os.path.isdir(static_tmp_path):
#             pass
#         else:
#             raise

# 首頁
# @app.route('/static/tmp/<path:filename>', methods=['GET'])
# def serve_audio(filename):
#     """供應音訊檔案 URL 的端點"""
#     return send_from_directory(app.config["UPLOAD_FOLDER"], filename)

@app.route("/callback", methods=['POST'])
def callback():
    # 取得 X-Line-Signature 標頭
    signature = request.headers.get('X-Line-Signature', '')

    # 取得請求內容
    body = request.get_data(as_text=True)
    app.logger.info(f"Request body: {body}")

    # 驗證並處理 Webhook
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

# 處理文字訊息
@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event: MessageEvent):
    global detected_language, language_isornot
    with ApiClient(api_config) as api_client:
        messaging_api = MessagingApi(api_client)
        user_message = event.message.text

        if user_message.startswith("@"):
            # 提取語音內容
            speech_text = user_message[1:].strip()
            audio_url = text_to_speech(speech_text)

            # 構造音訊檔案 URL
            # audio_url = f"{request.host_url}static/tmp/{os.path.basename(audio_path)}"

            # 回傳語音訊息
            messaging_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[
                        AudioMessage(
                            original_content_url=audio_url,
                            duration=5000  # 假設固定時長
                        )
                    ]
                )
            )
        else:
            language_reply = language_detection(user_message)
            if language_isornot is False:
                language_isornot = None
                function_reply = function_detection(user_message)
                messaging_api.reply_message(
                    ReplyMessageRequest(reply_token=event.reply_token, messages=function_reply)
                )
            else:
                messaging_api.reply_message(
                    ReplyMessageRequest(reply_token=event.reply_token, messages=language_reply)
                )

# 處理圖片訊息
@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image_message(event: MessageEvent):
    with ApiClient(api_config) as api_client:
        messaging_api = MessagingApi(api_client)
        # 未偵測到語言，則回覆提示
        if not detected_language:
            messaging_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text='請先輸入欲翻譯成的語言\u000A\u000APlease enter the language you want to translate to first.')]
                )
            )
            return

        # 取得圖片
        with ApiClient(api_config) as api_client:
            line_bot_blob_api = MessagingApiBlob(api_client)
            message_content = line_bot_blob_api.get_message_content(message_id=event.message.id)
            img = Image.open(io.BytesIO(message_content))
            with ApiClient(api_config) as api_client:
                messaging_api = MessagingApi(api_client)
                food_reply = food_detection(detected_language, img)
                messaging_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=food_reply)
                    )
                

            
            # 將圖片存在暫存資料夾
            # with tempfile.NamedTemporaryFile(dir=static_tmp_path, prefix='jpg' + '-', delete=False) as tf: # 以 jpg 為前綴建立暫存檔
            #     tf.write(message_content) # 將圖片寫入暫存檔
            #     tempfile_path = tf.name   # 取得暫存檔路徑

            # dist_path = tempfile_path + '.' + 'jpg'    # 將暫存檔路徑加上副檔名
            # dist_name = os.path.basename(dist_path)    # 取得暫存檔名稱
            # os.rename(tempfile_path, dist_path)        # 重新命名暫存檔
            # img_url = request.host_url + os.path.join('static', 'tmp', dist_name) # 取得圖片 URL
            # with ApiClient(api_config) as api_client:
            #     line_bot_api = MessagingApi(api_client)
                # line_bot_api.reply_message(
                #     ReplyMessageRequest(
                #         reply_token=event.reply_token,
                #         messages=[
                #             TextMessage(text='Save content.'),
                #             TextMessage(text=request.host_url + os.path.join('static', 'tmp', dist_name))
                #         ]
                #     )
                # )

# 處理音訊訊息
@handler.add(MessageEvent, message=AudioMessageContent)
def handle_audio_message(event: MessageEvent):
    with ApiClient(api_config) as api_client:
        line_bot_blob_api = MessagingApiBlob(api_client)
    temp_dir = tempfile.gettempdir()
    temp_audio_path = os.path.join(temp_dir, f"{event.message.id}.m4a")

    try:
        # 取得音訊內容
        message_content = line_bot_blob_api.get_message_content(event.message.id)
        with open(temp_audio_path, 'wb') as temp_audio_file:
            temp_audio_file.write(message_content.read())

        mime_type = "audio/mpeg"
        audio = genai.upload_file(temp_audio_path, mime_type=mime_type)
        response = model.generate_content([
            f"偵測音檔語言並用繁體中文列出該語言名稱，並將內容翻譯成繁體中文列出", audio
        ])
        line_bot_blob_api.reply_message(
            ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=response.text)])
        )
    except Exception as e:
        line_bot_blob_api.reply_message(
            ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=f"處理音檔時發生錯誤: {str(e)}")])
        )
    finally:
        if os.path.exists(temp_audio_path):
            os.remove(temp_audio_path)

# Azure Speech文字轉語音
def text_to_speech(text):
    # tmp_dir = "static/tmp"
    # os.makedirs(tmp_dir, exist_ok=True)
    tmp_dir = tempfile.gettempdir()
    output_file = os.path.join(tmp_dir, "output.wav")
    try:
        # 配置音訊輸出
        audio_config = speechsdk.audio.AudioOutputConfig(filename=output_file)
        synthesizer = speechsdk.SpeechSynthesizer(speech_config=speech_config, audio_config=audio_config)
        
        # 進行語音合成
        result = synthesizer.speak_text_async(text).get()
        
        if result.reason == speechsdk.ResultReason.Canceled:
            cancellation_details = result.cancellation_details
            raise Exception(f"Speech synthesis canceled: {cancellation_details.reason}")
        
        # 上傳至 Blob Storage
        blob_url = upload_to_blob(output_file, "output.wav")
        return blob_url
    except Exception as e:
        raise Exception(f"Error during text-to-speech: {e}")
    finally:
        if os.path.exists(output_file):
            os.remove(output_file)
    # 配置音訊輸出
    # audio_config = speechsdk.audio.AudioOutputConfig(filename=output_file)
    # synthesizer = speechsdk.SpeechSynthesizer(speech_config=speech_config, audio_config=audio_config)

    # # 進行語音合成
    # result = synthesizer.speak_text_async(text).get()

    # if result.reason == result.reason.Canceled:
    #     cancellation_details = result.cancellation_details
    #     raise Exception(f"Speech synthesis canceled: {cancellation_details.reason}")

    # return upload_to_blob(output_file, "output.wav")

# 判斷語言名稱
def language_detection(language_msg):
    global detected_language, language_isornot, function_type
    response_language = model.generate_content([f"判斷 {language_msg} 是否是語言名稱，並簡答是或不是"])
    reply = response_language.text.replace('*', '').replace('\n', '')

    if reply == '是':
        response_language = model.generate_content([f"判斷{language_msg}這個詞彙名稱上是什麼語言，如為中文，請區分是繁體還是簡體，並用繁體中文簡答語言名稱即可"])
        tran_language = response_language.text.replace('*', '').replace('\n', '')
        detected_language = tran_language

        if function_type == '翻譯':
            excute_sentence1 = f'對話內容將使用{tran_language}進行'
            excute_sentence2 = '請選擇或拍攝一張菜單照片'
            response1 = translation_function(tran_language, excute_sentence1)
            response2 = translation_function(tran_language, excute_sentence2)

            return [
                TextMessage(text=response1.text.replace('*', '').replace('\n', '')),
                TextMessage(text=response2.text.replace('*', '').replace('\n', ''))
            ]
        else:
            excute_sentence1 = f'語音將以{tran_language}表達'
            excute_sentence2 = '請輸入想要翻譯的文字，並在文字前加上@符號'
            excute_sentence3 = '範例 - @我想點牛排'
            response1 = translation_function(tran_language, excute_sentence1)
            response2 = translation_function(tran_language, excute_sentence2)
            response3 = translation_function(tran_language, excute_sentence3)
            return [
                TextMessage(text=response1.text.replace('*', '').replace('\n', '')),
                TextMessage(text=response2.text.replace('*', '').replace('\n', '')),
                TextMessage(text=response3.text.replace('*', '').replace('\n', ''))
            ]
    else:
        language_isornot = False
        return [TextMessage(text="請輸入語言名稱")]
    
# 翻譯功能
def translation_function(tran_language, sentences):
    response = model.generate_content([f"使用{tran_language}翻譯{sentences} 這個句子，且前後不加其他語句"])#把{sentences}這句話翻譯成{tran_language}
    return response

# 功能檢測
def function_detection(function_msg):
    global function_type
    # 記錄當前使用功能
    function_type = function_msg
    if function_msg == '翻譯':
        # 如果已有偵測到語言，則回覆提示
        pass
        return [
            TextMessage(
                text="這是菜單翻譯功能$\u000A\u000AThis is a menu translation feature.",
                emojis=[
                    {
                        'index': 8,  # 表情符號在文字中的插入位置
                        'productId': '5ac1bfd5040ab15980c9b435',  # LINE 官方表情包的 ID
                        'emojiId': '012'  # 表情符號的 ID
                    }
                ]),  
            TextMessage(text='請輸入欲翻譯成的語言\u000A\u000APlease enter the language you want to translate to.'),
            StickerMessage(package_id="11539", sticker_id="52114110")
        ]
    elif function_msg == '語音':    
        return [
            TextMessage(text="這是可以協助點餐的語音功能"),
            TextMessage(text="請輸入想要語音輸出的語言")
        ]
    else:
        return [TextMessage(text="目前不提供此服務")]

# 料理名稱辨識
def food_detection(tran_language, image):
    # global detected_language, language_isornot
    response_food = model.generate_content([f"判斷圖片中是否包含料理名稱的文字，並簡答是或不是", image])
    # response_food = model.generate_content([f"列出圖片中是料理名稱的文字，前面加上數字編號", image])
    reply = response_food.text.replace('*', '').replace('\n', '')

    if reply == '是':
        response_food = model.generate_content([f"列出圖片中是料理名稱的文字，前面加上數字編號，前後不加任何其他無相干文字", image])
        # response_food = model.generate_content([f"將數字當作Key，然後Values是圖片中為料理名稱的文字，並寫成字典格式", image])
        

        # class food_content(typing.TypedDict):
        #     food_original_language_name: str
        #     food_translation_name: str
        #     ingredients: list[str]

        # # model = genai.GenerativeModel("gemini-1.5-pro-latest")
        # result = model.generate_content([
        #     f"列出料理名稱及可能包含的食物有哪些，並用{tran_language}翻譯",image],
        #     generation_config=genai.GenerationConfig(
        #         response_mime_type="application/json", response_schema=list[food_content]
        #     ),
        # )
        # print(result.text)
        reply_food = response_food.text.replace('*', '')
        response = translation_function(tran_language, reply_food)

        return [TextMessage(text=response.text)] #.replace('*', '')
    else:
        # language_isornot = False
        response = translation_function(tran_language, "圖片中未偵測到料理名稱")
        return [TextMessage(text=response.text.replace('*', '').replace('\n', ''))]

# 上傳檔案到 Azure Blob Storage
def upload_to_blob(file_path, blob_name):
    # with open(file_path, "rb") as file_data:
    #     blob_client = blob_container_client.get_blob_client(blob_name)
    #     blob_client.upload_blob(file_data, overwrite=True)
    # return f"https://{blob_service_client.account_name}.blob.core.windows.net/{BLOB_CONTAINER_NAME}/{blob_name}"
    try:
        with open(file_path, "rb") as file_data:
            blob_client = blob_container_client.get_blob_client(blob_name)
            blob_client.upload_blob(file_data, overwrite=True)
        return f"https://{blob_service_client.account_name}.blob.core.windows.net/{BLOB_CONTAINER_NAME}/{blob_name}"
    except Exception as e:
        raise Exception(f"Failed to upload to Azure Blob Storage: {e}")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    # make_static_tmp_dir()
    app.run(host="0.0.0.0", port=port, debug=True)
