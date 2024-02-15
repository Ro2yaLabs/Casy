import os
import cv2
import numpy as np
import torch
import time

import platform
import subprocess
import argparse
from tqdm import tqdm
from .models import Wav2Lip
from ultralytics import YOLO
from random import randint
from PIL import Image, ImageTk
import tkinter as tk
from . import audio
import matplotlib.pyplot as plt


mel_step_size = 16
device = 'cpu'

yolo = YOLO('wav2lip_master/yolo/best.pt')


def get_smoothened_boxes(boxes, T):
	"""
    Smooth the bounding boxes over a temporal window.
    """
	for i in range(len(boxes)):
		if i + T > len(boxes):
			window = boxes[len(boxes) - T:]
		else:
			window = boxes[i : i + T]
		boxes[i] = np.mean(window, axis=0)
	return boxes

def face_detect(images, args):
	"""
    Detect faces in a batch of images using YOLO.
    """
	batch_size = args.face_det_batch_size
	# batch_size = 1
	
	while 1:
		predictions = []
		try:
			for i in range(0, len(images), batch_size):
				results = yolo.predict(images[i:i + batch_size], verbose=False)
				try:
					boxes = results[0].boxes.cpu().xyxy[0].tolist()
					predictions.append(boxes)
				except Exception as e:
					cv2.imwrite(f"temp/faulty_frame{randint(0, 10000)}.jpg", images[0])
					print("face not detected")
				
		except RuntimeError:
			if batch_size == 1: 
				raise RuntimeError('Image too big to run face detection on GPU. Please use the --resize_factor argument')
			batch_size //= 2
			print('Recovering from OOM error; New batch size: {}'.format(batch_size))
			continue
		break

	results = []
	pady1, pady2, padx1, padx2 = args.pads
	for rect, image in zip(predictions, images):
		if rect is None:
			cv2.imwrite('temp/faulty_frame.jpg', image) # check this frame where the face was not detected.
			raise ValueError('Face not detected! Ensure the video contains a face in all the frames.')
		
		y1 = max(0, int(rect[1]) - pady1)
		y2 = min(image.shape[0], int(rect[3]) + pady2)
		x1 = max(0, int(rect[0]) - padx1)
		x2 = min(image.shape[1], int(rect[2]) + padx2)
		
		results.append([x1, y1, x2, y2])

	boxes = np.array(results)
	if not args.nosmooth: 
		boxes = get_smoothened_boxes(boxes, T=5)
	results = [[image[y1: y2, x1:x2], (y1, y2, x1, x2)] for image, (x1, y1, x2, y2) in zip(images, boxes)]

	return results 

def datagen(mels, args):
	"""
    Data generator for processing batches.
    """
	img_batch, mel_batch, frame_batch, coords_batch = [], [], [], []

	reader = read_frames(args)
	t = []
	prev = None
	for i, m in enumerate(mels):
		try:
			frame_to_save = next(reader)
		except StopIteration:
			reader = read_frames(args)
			frame_to_save = next(reader)
		
		s = time.time()
		try:
			prev = face_detect([frame_to_save], args)[0]
			face, coords = prev
		except:
			face, coords = prev
		e = time.time()
		t.append(e-s)

		face = cv2.resize(face, (args.img_size, args.img_size))
			
		if i%10000 == 0:
			cv2.imwrite(f"test{i}.jpg", face)

		img_batch.append(face)
		mel_batch.append(m)
		frame_batch.append(frame_to_save)
		coords_batch.append(coords)

		if len(img_batch) >= args.wav2lip_batch_size:
			img_batch, mel_batch = np.asarray(img_batch), np.asarray(mel_batch)

			img_masked = img_batch.copy()
			img_masked[:, args.img_size//2:] = 0

			img_batch = np.concatenate((img_masked, img_batch), axis=3) / 255.
			mel_batch = np.reshape(mel_batch, [len(mel_batch), mel_batch.shape[1], mel_batch.shape[2], 1])

			yield img_batch, mel_batch, frame_batch, coords_batch
			img_batch, mel_batch, frame_batch, coords_batch = [], [], [], []

	ss = sum(t)
	print(f"avg: {ss/len(t)}")
	print(f"total for face detection: {ss}")

	if len(img_batch) > 0:
		img_batch, mel_batch = np.asarray(img_batch), np.asarray(mel_batch)

		img_masked = img_batch.copy()
		img_masked[:, args.img_size//2:] = 0

		img_batch = np.concatenate((img_masked, img_batch), axis=3) / 255.
		mel_batch = np.reshape(mel_batch, [len(mel_batch), mel_batch.shape[1], mel_batch.shape[2], 1])

		yield img_batch, mel_batch, frame_batch, coords_batch

def _load(checkpoint_path):
	"""
    Load the checkpoint.
    """
	if device == 'cuda':
		checkpoint = torch.load(checkpoint_path)
	else:
		checkpoint = torch.load(checkpoint_path,
								map_location=lambda storage, loc: storage)
	return checkpoint

def load_model(path):
	"""
    Load the Wav2Lip model.
    """
	model = Wav2Lip()
	print("Load checkpoint from: {}".format(path))
	checkpoint = _load(path)
	s = checkpoint["state_dict"]
	new_s = {}
	for k, v in s.items():
		new_s[k.replace('module.', '')] = v
	model.load_state_dict(new_s)

	model = model.to(device)
	return model.eval()

def read_frames():
    """
    Read frames from a folder of image files.
    """
	
    image_files = [f for f in os.listdir(args.frame_path) if f.split('.')[-1].lower() in ['jpg', 'png', 'jpeg']]
    image_files.sort()

    for image_file in image_files:
        image_path = os.path.join(args.frame_path, image_file)
        frame = cv2.imread(image_path)
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

        yield frame

def main(args):
	s = time.time()
	if not os.path.isfile(args.face):
		raise ValueError('--face argument must be a valid path to video/image file')

	elif args.face.split('.')[1] in ['jpg', 'png', 'jpeg']:
		fps = args.fps
	else:
		video_stream = cv2.VideoCapture(args.face)
		fps = video_stream.get(cv2.CAP_PROP_FPS)
		video_stream.release()


	if not args.audio.endswith('.wav'):
		print('Extracting raw audio...')
		command = 'ffmpeg -y -i {} -strict -2 {}'.format(args.audio, 'temp/temp.wav')

		subprocess.call(command, shell=True)
		args.audio = 'temp/temp.wav'

	wav = audio.load_wav(args.audio, 16000)
	mel = audio.melspectrogram(wav)
	print(mel.shape)

	if np.isnan(mel.reshape(-1)).sum() > 0:
		raise ValueError('Mel contains nan! Using a TTS voice? Add a small epsilon noise to the wav file and try again')

	mel_chunks = []
	mel_idx_multiplier = 80./fps 
	i = 0
	while 1:
		start_idx = int(i * mel_idx_multiplier)
		if start_idx + mel_step_size > len(mel[0]):
			mel_chunks.append(mel[:, len(mel[0]) - mel_step_size:])
			break
		mel_chunks.append(mel[:, start_idx : start_idx + mel_step_size])
		i += 1

	print("Length of mel chunks: {}".format(len(mel_chunks)))

	batch_size = args.wav2lip_batch_size
	gen = datagen(mel_chunks, args)

	root = tk.Tk()
	root.title("Image Display")
	label = tk.Label(root)
	label.pack()

	def display_image_in_tkinter(img):
		img = Image.fromarray(img.astype('uint8'), 'RGB')
		tkimage = ImageTk.PhotoImage(image=img)
		label.config(image=tkimage)
		label.image = tkimage

	for i, (img_batch, mel_batch, frames, coords) in enumerate(tqdm(gen, 
											total=int(np.ceil(float(len(mel_chunks))/batch_size)))):
		if i == 0:
			model = load_model(args.checkpoint_path)
			print ("Model loaded")

			# out = cv2.VideoWriter('temp/result.avi', 
			# 						cv2.VideoWriter_fourcc(*'DIVX'), fps, (720, 720))

		img_batch = torch.FloatTensor(np.transpose(img_batch, (0, 3, 1, 2))).to(device)
		mel_batch = torch.FloatTensor(np.transpose(mel_batch, (0, 3, 1, 2))).to(device)

		with torch.no_grad():
			pred = model(mel_batch, img_batch)

		pred = pred.cpu().numpy().transpose(0, 2, 3, 1) * 255.
		
		for p, f, c in zip(pred, frames, coords):
			y1, y2, x1, x2 = c

			p = cv2.resize(p.astype(np.uint8), (x2 - x1, y2 - y1))
			f[y1:y2, x1:x2] = p
			
			# plt.imshow(f)
			try:
				display_image_in_tkinter(f)
			except:
				print("error in f")

		root.update_idletasks()
		root.update()


	command = 'ffmpeg -y -i {} -i {} -strict -2 -q:v 1 {}'.format(args.audio, 'temp/result.avi', args.outfile)
	subprocess.call(command, shell=platform.system() != 'Windows')

if __name__ == '__main__':
	parser = argparse.ArgumentParser(description='Inference code to lip-sync videos in the wild using Wav2Lip models')

	parser.add_argument('--checkpoint_path', type=str, 
						help='Name of saved checkpoint to load weights from', required=True)

	parser.add_argument('--face', type=str, 
						help='Filepath of video/image that contains faces to use', required=True)
	parser.add_argument('--audio', type=str, 
						help='Filepath of video/audio file to use as raw audio source', required=True)
	parser.add_argument('--outfile', type=str, help='Video path to save result. See default for an e.g.', 
									default='results/result_voice.mp4')


	parser.add_argument('--static', type=bool, 
						help='If True, then use only first video frame for inference', default=False)
	parser.add_argument('--fps', type=float, help='Can be specified only if input is a static image (default: 25)', 
						default=25., required=False)

	parser.add_argument('--pads', nargs='+', type=int, default=[0, 0, 0, 0], 
						help='Padding (top, bottom, left, right). Please adjust to include chin at least')

	parser.add_argument('--face_det_batch_size', type=int, 
						help='Batch size for face detection', default=16)
	parser.add_argument('--wav2lip_batch_size', type=int, help='Batch size for Wav2Lip model(s)', default=1)

	parser.add_argument('--resize_factor', default=1, type=int, 
				help='Reduce the resolution by this factor. Sometimes, best results are obtained at 480p or 720p')

	parser.add_argument('--crop', nargs='+', type=int, default=[0, -1, 0, -1], 
						help='Crop video to a smaller region (top, bottom, left, right). Applied after resize_factor and rotate arg. ' 
						'Useful if multiple face present. -1 implies the value will be auto-inferred based on height, width')

	parser.add_argument('--box', nargs='+', type=int, default=[-1, -1, -1, -1], 
						help='Specify a constant bounding box for the face. Use only as a last resort if the face is not detected.'
						'Also, might work only if the face is not moving around much. Syntax: (top, bottom, left, right).')

	parser.add_argument('--rotate', default=False, action='store_true',
						help='Sometimes videos taken from a phone can be flipped 90deg. If true, will flip video right by 90deg.'
						'Use if you get a flipped result, despite feeding a normal looking video')

	parser.add_argument('--nosmooth', default=False, action='store_true',
						help='Prevent smoothing face detections over a short temporal window')

	parser.add_argument('--save_frames', default=False, action='store_true',
						help='Save each frame as an image. Use with caution')
	parser.add_argument('--gt_path', type=str, 
						help='Where to store saved ground truth frames', required=False)
	parser.add_argument('--pred_path', type=str, 
						help='Where to store frames produced by algorithm', required=False)
	parser.add_argument('--save_as_video', action="store_true", default=False,
						help='Whether to save frames as video', required=False)
	parser.add_argument('--image_prefix', type=str, default="",
						help='Prefix to save frames with', required=False)

	args = parser.parse_args()
	args.img_size = 96
	mel_step_size = 16
	device = 'cuda' if torch.cuda.is_available() else 'cpu'
	print('Using {} for inference.'.format(device))

	yolo = YOLO('yolo/best.pt')

	if os.path.isfile(args.face) and args.face.split('.')[1] in ['jpg', 'png', 'jpeg']:
		args.static = True

	main(args)
