"""Conservative public projection of leased cursor and owned native telemetry."""
import copy


def project(status, cursor, max_games, observation=None):
    """Project an owner-thread snapshot; observation has already been verified.

    Cursor's current preset is the next position to be selected, not a claim
    about the map of an active match. Native counters are process snapshots,
    not durable outcome or error histories.
    """
    value=dict(status)
    value['status_code']=None
    cycle=list(cursor.cycle);index=cursor.state['index']
    value.update(map_cycle=cycle,index=index,current_preset=cycle[index],
                 next_preset=cycle[(index+1)%len(cycle)],max_matches=max_games,
                 active_matches=None,active_match_list=None,current_lobby=None,
                 finished_matches=None,error_count=None,rotation_errors=None,
                 availability={'cursor':'verified','max_matches':'verified' if max_games is not None else 'unavailable',
                               'active_matches':'unavailable','active_match_list':'unavailable',
                               'current_lobby':'unavailable','finished_matches':'unavailable',
                               'error_count':'unavailable','rotation_errors':'unavailable'})
    if observation is not None:
        event,stamp,catalog=observation
        value['_native_observed_at']=stamp
        value['active_matches']=event['active_games']
        value['availability']['active_matches']='verified_snapshot'
        telemetry=event.get('matches_v1')
        if telemetry is not None and catalog is not None and all(
                item['catalog_index'] is None or item['catalog_index']<len(catalog.entries)
                for item in telemetry['matches']):
            matches=[]
            for item in telemetry['matches']:
                index=item['catalog_index']
                preset,sha=(None,None) if index is None else catalog.entries[index]
                matches.append({**item,'match_id':item.get('match_id'),
                                'session_generation':catalog.generation,
                                'preset_id':preset,'map_sha256':sha})
            value['active_match_list']=matches
            value['finished_matches']=telemetry['retired_count']
            value['rotation_errors']=telemetry['rotation_error_count']
            for key in ('active_match_list','finished_matches','rotation_errors'):
                value['availability'][key]='verified_process_snapshot'
        if event['phase']=='IDLE':
            value['availability']['current_lobby']='none'
        elif event['phase'] in ('LOBBY','COUNTDOWN') and catalog is not None:
            record=catalog.data['pending']
            if record is not None:
                from .native import native_catalog_lobby_matches
                counter=event['host_counter']
                if ((record['counter'] is None or record['counter']==counter) and
                    native_catalog_lobby_matches(event,generation=catalog.generation,
                        request_id=record['request_id'],catalog_index=record['index'],host_counter=counter)):
                    value['current_lobby']={'preset_id':cycle[record['position']],
                        'catalog_index':record['index'],'host_counter':counter,'phase':event['phase']}
                    value['availability']['current_lobby']='verified_snapshot'
        if (max_games is not None and event['active_games']>=max_games
                and value.get('state') in ('waiting_native_ipc','blocked_pending','capacity_full')):
            value['state']='capacity_full'
            value['status_code']='CAPACITY_FULL'
    return value


def public(status, now, ttl):
    """Expire cached native snapshots even when the owner loop has stalled."""
    result=copy.deepcopy(status)
    for key in ('map_cycle','index','current_preset','next_preset','max_matches',
                'active_matches','active_match_list','current_lobby','finished_matches',
                'error_count','rotation_errors','last_transition','status_code'):
        result.setdefault(key,None)
    availability=result.setdefault('availability',{})
    for key in ('cursor','max_matches','active_matches','active_match_list','current_lobby',
                'finished_matches','error_count','rotation_errors','last_transition'):
        availability.setdefault(key,'unavailable')
    stamp=result.pop('_native_observed_at',None)
    if stamp is not None and (not 0 <= now-stamp <= ttl):
        result['active_matches']=None
        result['current_lobby']=None
        for key in ('active_match_list','finished_matches','rotation_errors'):
            result[key]=None
            result['availability'][key]='unavailable'
        result['availability']['active_matches']='unavailable'
        result['availability']['current_lobby']='unavailable'
        if result.get('state')=='capacity_full':
            result['state']='blocked_pending' if result.get('pending') is True else 'waiting_native_ipc'
            result['status_code']=None
    return result
