# coding=utf-8

from flask import Flask, request, Response, jsonify
import caffe
import numpy as np
import classify_nsfw
from werkzeug.utils import secure_filename
import os
import contextlib
import json
from io import BytesIO
import urllib2
import uuid
import logging
from datetime import datetime

def make_transformer(nsfw_net):
    transformer = caffe.io.Transformer({'data': nsfw_net.blobs['data'].data.shape})
    transformer.set_transpose('data', (2, 0, 1))
    transformer.set_mean('data', np.array([104, 117, 123]))
    transformer.set_raw_scale('data', 255)
    transformer.set_channel_swap('data', (2, 1, 0))
    return transformer

nsfw_net = caffe.Net(
    "/opt/open_nsfw/nsfw_model/deploy.prototxt",
    "/opt/open_nsfw/nsfw_model/resnet_50_1by2_nsfw.caffemodel",
    caffe.TEST
)

caffe_transformer = make_transformer(nsfw_net)
app = Flask(__name__)


#1 将审核的临时图片文件放到硬盘 2 图片放到内存
FILE_PROCESSING_MODE = int(os.getenv('FILE_PROCESSING_MODE', '1'))  # Default to 1 if not set

# 保存在硬盘中的路径
IMAGE_DIR = '/opt/web/images'

# Set logging
log_dir = "log"
if not os.path.exists(log_dir):
    os.makedirs(log_dir)

# Normal operation log
log_handler = logging.FileHandler("{}/app_{}.log".format(log_dir, datetime.now().strftime('%Y-%m-%d')))
log_handler.setLevel(logging.INFO)
app.logger.addHandler(log_handler)

# Error log
error_log_handler = logging.FileHandler("{}/error_{}.log".format(log_dir, datetime.now().strftime('%Y-%m-%d')))
error_log_handler.setLevel(logging.ERROR)
app.logger.addHandler(error_log_handler)


def process_file(file):
    ext = file.filename.rsplit('.', 1)[-1] 
    filename = str(uuid.uuid4()) + '.' + ext 
    filename = secure_filename(filename)

    if FILE_PROCESSING_MODE == 1:
        file_path = os.path.join(IMAGE_DIR, filename)
        if not os.path.exists(IMAGE_DIR):
            os.makedirs(IMAGE_DIR)
        file.save(file_path)
        app.logger.info("File saved at {}".format(file_path))
        return open(file_path, 'rb'), file_path

    elif FILE_PROCESSING_MODE == 2:
        file_obj = BytesIO()
        file.save(file_obj)
        file_obj.seek(0)
        app.logger.info("File saved in memory")
        return file_obj, None

@app.route('/', methods=['POST', 'GET'])
def single_classify():
    if request.method == 'POST':
        if 'image' not in request.files:
            return jsonify({'error': 'No image in request'}), 410
        file = request.files['image']
        if file.filename == '':
            return jsonify({'error': 'No selected file'}), 410
        
        file_obj, file_path = process_file(file)
        image_data = file_obj.read()

        try:
            score = classify(image_data, nsfw_net)
        except Exception as e:
            if file_obj:
                file_obj.close()
            if file_path is not None:
                if os.path.exists(file_path):
                    os.remove(file_path)
            app.logger.error("Error during classification: {}".format(str(e)))
            return jsonify({'error': 'Error during classification: {}'.format(str(e))}), 500

        file_obj.close()
        if file_path is not None:
            if os.path.exists(file_path):
                os.remove(file_path)
        
        return jsonify({'score': score})

    elif request.method == 'GET':
        if 'url' in request.args:
            single_image = {'url': request.args.get('url')}
            result = classify_from_url(single_image, nsfw_net)
            return jsonify(result)
        else:
            return jsonify({'error': 'Missing url parameter'}), 400

@app.route('/batch-classify', methods=['POST'])
def batch_classify():
    req_json = request.get_json(force=True)

    if "urls" in req_json:
        image_entries = list(map(lambda u: {'url': u}, req_json["urls"]))
    elif "images" in req_json:
        image_entries = req_json["images"]
    else:
        return jsonify({'error': 'Accepted formats are {"urls": ["url1", "url2"]} or {"images": [{"url":"url1"}, {"url":"url2"}]}'}), 400

    def stream_predictions():
        predictions = classify_from_urls(image_entries).__iter__()
        try:
            prev_prediction = next(predictions)
        except StopIteration:
            yield '{"predictions": []}'
            raise StopIteration
        yield '{"predictions": [\n'
        for prediction in predictions:
            yield json.dumps(prev_prediction) + ',\n'
            prev_prediction = prediction
        yield json.dumps(prev_prediction) + '\n]}'

    return Response(stream_predictions(), mimetype='application/json')

def classify_from_url(image_entry, nsfw_net):
    headers = {'User-agent': 'Mozilla/5.0 (Windows; U; Windows NT 5.1; de; rv:1.9.1.5) Gecko/20091102 Firefox/3.5.5'}
    result = {}
    try:
        req = urllib2.Request(image_entry["url"], None, headers)
        with contextlib.closing(urllib2.urlopen(req)) as stream:
            try:
                score = classify(stream.read(), nsfw_net)
                result = {'score': score}
            except Exception as e:
                result = {'error_code': 500, 'error_reason': 'Error during classification: {}'.format(str(e))}
    except urllib2.HTTPError as e:
        result = {'error_code': e.code, 'error_reason': str(e)}
    except urllib2.URLError as e:
        result = {'error_code': 500, 'error_reason': 'Error during classification: {}'.format(str(e))}
    except Exception as e:
        result = {'error_code': 500, 'error_reason': 'Error during URL retrieval: {}'.format(str(e))}

    result.update(image_entry)
    return result

def classify(image_data, nsfw_net):
    try:
        scores = classify_nsfw.caffe_preprocess_and_compute(
            image_data,
            caffe_transformer=caffe_transformer,
            caffe_net=nsfw_net,
            output_layers=['prob']
        )
        return scores[1]
    except Exception as e:
        raise Exception('Error during image classification: {}'.format(str(e)))

def classify_from_urls(image_entries):
    for e in image_entries:
        yield classify_from_url(e, nsfw_net)

if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=False)
