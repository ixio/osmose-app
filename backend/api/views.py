import csv
from datetime import datetime

from django.http import Http404, HttpResponse
from django.utils.http import urlquote
from django.shortcuts import get_object_or_404

from django.utils.dateparse import parse_datetime
from datetime import timedelta

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.decorators import api_view

from django.db import transaction
from django.db.models import Count, F
from backend.api.models import Dataset, AnnotationSet, AnnotationCampaign, AnnotationTask, AnnotationResult, DatasetType, AudioMetadatum, GeoMetadatum
from django.contrib.auth.models import User

from backend.settings import STATIC_URL, DATASET_IMPORT_FOLDER, DATASET_FILES_FOLDER, DATASET_SPECTRO_FOLDER

@api_view(http_method_names=['GET'])
def dataset_index(request):
    datasets = Dataset.objects.annotate(Count('files')).select_related('dataset_type')
    return Response([{
        'id': dataset.id,
        'name': dataset.name,
        'files_type': dataset.files_type,
        'start_date': dataset.start_date,
        'end_date': dataset.end_date,
        'files_count': dataset.files__count,
        'type': dataset.dataset_type.name
    } for dataset in datasets])

@api_view(http_method_names=['GET'])
def user_index(request):
    return Response([{"id": user.id, "email": user.email} for user in User.objects.all()])

@api_view(http_method_names=['GET'])
def annotation_campaign_show(request, campaign_id):
    campaign = AnnotationCampaign.objects.get(id=campaign_id)
    user_tasks = campaign.tasks.filter(annotator_id=request.user.id)
    return Response({
        'campaign': {
            'id': campaign.id,
            'name': campaign.name,
            'desc': campaign.desc,
            'instructionsUrl': campaign.instructions_url,
            'start': campaign.start,
            'end': campaign.end,
            'annotation_set_id': campaign.annotation_set_id,
            'owner_id': campaign.owner_id,
        },
        'tasks': [res for res in campaign.tasks.values('status', 'annotator_id').annotate(count=Count('status'))]
    })

@transaction.atomic
@api_view(http_method_names=['GET', 'POST'])
def annotation_campaign_index_create(request):
    if request.method == 'POST':
        campaign = AnnotationCampaign(
            name=request.data['name'],
            desc=request.data['desc'],
            start=request.data['start'],
            end=request.data['end'],
            annotation_set_id=request.data['annotation_set'],
            owner_id=request.user.id
        )
        campaign.save()
        campaign.datasets.set(Dataset.objects.filter(id__in=request.data['datasets']))
        file_count = sum(campaign.datasets.annotate(Count('files')).values_list('files__count', flat=True))
        total_goal = file_count * int(request.data['annotation_goal'])
        annotator_goal, remainder = divmod(total_goal, len(request.data['annotators']))
        annotation_method = ['random', 'sequential'][int(request.data['annotation_method'])]
        for annotator in User.objects.filter(id__in=request.data['annotators']):
            files_target = annotator_goal
            if remainder > 0:
                files_target += 1
                remainder -= 1
            campaign.add_annotator(annotator, files_target, annotation_method)
        return Response({'id': campaign.id})

    campaigns = AnnotationCampaign.objects.annotate(Count('datasets')).prefetch_related('tasks')
    return Response([{
        'id': campaign.id,
        'name': campaign.name,
        'instructionsUrl': campaign.instructions_url,
        'start': campaign.start,
        'end': campaign.end,
        'annotation_set_id': campaign.annotation_set_id,
        'tasks_count': campaign.tasks.count(),
        'user_tasks_count': campaign.tasks.filter(annotator_id=request.user.id).count(),
        'complete_tasks_count': campaign.tasks.filter(status=2).count(),
        'user_complete_tasks_count': campaign.tasks.filter(annotator_id=request.user.id, status=2).count(),
        'datasets_count': campaign.datasets__count
    } for campaign in campaigns])

@api_view(http_method_names=['GET'])
def annotation_campaign_report_show(request, campaign_id):
    campaign = get_object_or_404(AnnotationCampaign, pk=campaign_id)
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="{campaign.name.replace(" ", "_")}.csv"'
    response.write('dataset,filename,start_time,end_time,start_frequency,end_frequency,annotation,annotator\n')
    results = AnnotationResult.objects.prefetch_related(
        'annotation_task',
        'annotation_task__annotator',
        'annotation_task__dataset_file',
        'annotation_task__dataset_file__dataset',
        'annotation_tag'
    ).filter(annotation_task__annotation_campaign_id=campaign_id)
    for result in results:
        line = [
            result.annotation_task.dataset_file.dataset.name,
            result.annotation_task.dataset_file.filename,
            str(result.start_time or ''),
            str(result.end_time or ''),
            str(result.start_frequency or ''),
            str(result.end_frequency or ''),
            result.annotation_tag.name,
            result.annotation_task.annotator.username
        ]
        response.write(','.join(line) + '\n')
    return response

@api_view(http_method_names=['GET'])
def annotation_set_index(request):
    annotation_sets = AnnotationSet.objects.all()
    return Response([{
        'id': annotation_set.id,
        'name': annotation_set.name,
        'desc': annotation_set.desc,
        'tags': list(annotation_set.tags.values_list('name', flat=True))
    } for annotation_set in annotation_sets])

@api_view(http_method_names=['GET'])
def annotation_task_index(request, campaign_id):
    tasks = AnnotationTask.objects.filter(
        annotator_id=request.user.id,
        annotation_campaign_id=campaign_id
    ).prefetch_related('dataset_file', 'dataset_file__dataset', 'dataset_file__audio_metadatum')
    return Response([{
        'id': task.id,
        'status': task.status,
        'filename': task.dataset_file.filename,
        'dataset_name': task.dataset_file.dataset.name,
        'start': task.dataset_file.audio_metadatum.start,
        'end': task.dataset_file.audio_metadatum.end
    } for task in tasks])

@api_view(http_method_names=['GET'])
def annotation_task_show(request, task_id):
    task = AnnotationTask.objects.prefetch_related(
        'annotation_campaign',
        'annotation_campaign__spectro_configs',
        'annotation_campaign__annotation_set',
        'dataset_file__audio_metadatum',
        'dataset_file__dataset',
        'dataset_file__dataset__spectro_configs',
        'dataset_file__dataset__audio_metadatum'
    ).get(id=task_id)
    df_sample_rate = task.dataset_file.audio_metadatum.sample_rate_khz
    ds_sample_rate = task.dataset_file.dataset.audio_metadatum.sample_rate_khz
    sample_rate = df_sample_rate if df_sample_rate else ds_sample_rate
    root_url = STATIC_URL + task.dataset_file.dataset.dataset_path
    spectros_configs = set(task.dataset_file.dataset.spectro_configs.all()) & set(task.annotation_campaign.spectro_configs.all())
    # We should probably use filename for sound_name but this allows to simplify seeding
    dataset_conf, sound_path = task.dataset_file.filepath.split('/')[-2:]
    sound_name = sound_path.replace('.wav', '')
    spectro_urls = [{
        'nfft': spectro_config.nfft,
        'winsize': spectro_config.window_size,
        'overlap': spectro_config.overlap,
        'urls': [
            urlquote(f'{root_url}/analysis/spectrograms/{dataset_conf}/{spectro_config.name}/{sound_name}/{tile}')
        for tile in spectro_config.zoom_tiles(sound_name)]
    } for spectro_config in spectros_configs]
    prev_annotations = task.results.values(
        'id',
        annotation=F('annotation_tag__name'),
        startTime=F('start_time'),
        endTime=F('end_time'),
        startFrequency=F('start_frequency'),
        endFrequency=F('end_frequency')
    )
    return Response({
        'task': {
            'campaignId': task.annotation_campaign_id,
            'annotationTags': list(task.annotation_campaign.annotation_set.tags.values_list('name', flat=True)),
            'boundaries': {
                'startTime': task.dataset_file.audio_metadatum.start,
                'endTime': task.dataset_file.audio_metadatum.end,
                'startFrequency': 0,
                'endFrequency': sample_rate / 2
            },
            'audioUrl': f'{root_url}/{task.dataset_file.filepath}',
            'audioRate': sample_rate,
            'spectroUrls': spectro_urls,
            'prevAnnotations': prev_annotations
    }})

@api_view(http_method_names=['POST'])
def annotation_task_update(request, task_id):
    task = get_object_or_404(AnnotationTask, pk=task_id)
    if task.annotator_id != request.user.id:
        raise Http404()
    tags = dict(map(reversed, task.annotation_campaign.annotation_set.tags.values_list('id', 'name')))
    for annotation in request.data['annotations']:
        task.results.create(
            start_time=annotation.get('startTime'),
            end_time=annotation.get('endTime'),
            start_frequency=annotation.get('start_frequency'),
            end_frequency=annotation.get('end_frequency'),
            annotation_tag_id=tags[annotation['annotation']]
        )
    task.sessions.create(
        start=datetime.fromtimestamp(request.data['task_start_time']),
        end=datetime.fromtimestamp(request.data['task_end_time']),
        session_output=request.data
    )
    task.status = 2
    task.save()
    next_task = AnnotationTask.objects.filter(
        annotator_id=request.user.id,
        annotation_campaign_id=task.annotation_campaign_id
    ).exclude(status=2).order_by('dataset_file__audio_metadatum__start').first()
    if next_task is None:
        return Response({'next_task': None, 'campaign_id': task.annotation_campaign_id})
    return Response({'next_task': next_task.id})

@api_view(http_method_names=['GET'])
def is_staff(request):
    return Response({'is_staff': request.user.is_staff})

@transaction.atomic
@api_view(http_method_names=['GET'])
def dataset_import(request):
    if not request.user.is_staff:
        return HttpResponse('Unauthorized', status=401)

    dataset_names = Dataset.objects.values_list('name', flat=True)
    new_datasets = []

    # TODO: we should also check for new spectros in existing datasets (or dataset update)
    # Check for new datasets
    with open(DATASET_IMPORT_FOLDER / 'datasets.csv') as csvfile:
        for dataset in csv.DictReader(csvfile):
            if dataset['name'] not in dataset_names:
                new_datasets.append(dataset)

    for dataset in new_datasets:
        # We need to split dataset name into a correct folder : dataset name + subfolder name
        name_root, *name_params = dataset['name'].split('_')
        name_params = '_'.join(name_params)

        # Create dataset metadata
        datatype = DatasetType.objects.filter(name=dataset['dataset_type_name']).first() # double check if nothing else better
        if not datatype:
            datatype = DatasetType.objects.create(
                name=dataset['dataset_type_name'],
                desc=dataset['dataset_type_desc'],
            )
        with open(DATASET_IMPORT_FOLDER / name_root / 'raw/metadata.csv') as csvfile:
             audio_raw = list(csv.DictReader(csvfile))[0]
        audio_metadatum = AudioMetadatum.objects.create(
            num_channels=audio_raw['nchannels'],
            sample_rate_khz=audio_raw['orig_fs'],
            sample_bits=audio_raw['sound_sample_size_in_bits'],
            start=parse_datetime(audio_raw['start_date']),
            end=parse_datetime(audio_raw['end_date'])
        )
        geo_metadatum = GeoMetadatum.objects.create(
            name=dataset['location_name'],
            desc=dataset['location_desc']
        )

        # Create dataset
        curr_dataset = Dataset.objects.create(
            name=dataset['name'],
            dataset_path=DATASET_IMPORT_FOLDER / name_root / 'raw/audio' / name_params,
            status=1,
            files_type=dataset['files_type'],
            dataset_type=datatype,
            start_date=audio_metadatum.start.date(),
            end_date=audio_metadatum.end.date(),
            audio_metadatum=audio_metadatum,
            geo_metadatum=geo_metadatum,
            owner=request.user,
        )

        # Create dataset_files
        with open(DATASET_IMPORT_FOLDER / name_root / 'raw/audio/timestamp.csv') as csvfile:
            for timestamp_data in csv.reader(csvfile):
                filename, start_timestamp = timestamp_data
                start = parse_datetime(start_timestamp)
                audio_metadatum = AudioMetadatum.objects.create(start=start, end=(start + timedelta(seconds=float(audio_raw['audioFile_duration']))))
                curr_dataset_file = curr_dataset.files.create(
                    filename=filename,
                    filepath=DATASET_IMPORT_FOLDER / name_root / 'raw/audio' / name_params / filename,
                    size=0,
                    audio_metadatum=audio_metadatum
                )

        # We should run import spectros on all datasets after dataset creation
        curr_spectros = curr_dataset.spectro_configs.values_list('name', flat=True)
        with open(DATASET_IMPORT_FOLDER / name_root / DATASET_SPECTRO_FOLDER / name_params / 'spectrograms.csv') as csvfile:
            for spectro in csv.DictReader(csvfile):
                if spectro['name'] not in curr_spectros:
                    curr_dataset.spectro_configs.create(**spectro)

    return Response('ok')
