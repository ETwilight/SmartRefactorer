"""Append-only evidence and token accounting. No planning or model calls."""
import copy
import hashlib
from datetime import datetime, timezone
import json
from pathlib import Path
import time
import threading
import uuid


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def _request_digest(request):
    return hashlib.sha256(json.dumps(request,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()


def pure_text_generation(request):
    """Only the built-in, stateless Responses JSON text request has this proof.

    This is deliberately an allowlist: adding provider tools or other modalities
    requires a separate accounting and side-effect analysis.
    """
    allowed={'model','reasoning','store','input','prompt_cache_options','max_output_tokens','text'}
    if not isinstance(request,dict) or set(request)-allowed or request.get('store') is not False:
        return False
    if request.get('model')!='gpt-6.1-sol':return False
    text=request.get('text');format_spec=text.get('format') if isinstance(text,dict) else None
    if not isinstance(format_spec,dict) or format_spec.get('type')!='json_schema':return False
    inputs=request.get('input')
    if not isinstance(inputs,list) or not inputs:return False
    for message in inputs:
        if not isinstance(message,dict) or set(message)-{'role','content'}:return False
        content=message.get('content')
        if isinstance(content,str):continue
        if not isinstance(content,list):return False
        for part in content:
            if not isinstance(part,dict) or part.get('type')!='input_text' or not isinstance(part.get('text'),str):
                return False
            if set(part)-{'type','text','prompt_cache_breakpoint'}:return False
    return True


def client_function_generation(request):
    """Stateless text/native function generation, with no provider-executed tools.

    Function calls are returned to the application; this proof applies only to
    generating them, never to executing/replaying a function. Reject references,
    hosted tools, background work and non-text inputs rather than guessing bounds.
    """
    allowed={'model','reasoning','store','input','max_output_tokens','text','tools','tool_choice',
             'parallel_tool_calls','include','instructions','truncation','metadata','prompt_cache_key',
             'prompt_cache_retention','temperature','top_p','service_tier','stream'}
    if not isinstance(request,dict) or set(request)-allowed or request.get('store') is not False:return False
    if request.get('stream',False) is not False:return False
    if request.get('model')!='gpt-6.1-sol':return False
    included=request.get('include',[])
    if not isinstance(included,list) or any(v!='reasoning.encrypted_content' for v in included):return False
    if 'instructions' in request and not isinstance(request['instructions'],str):return False
    tools=request.get('tools',[])
    if not isinstance(tools,list):return False
    for tool in tools:
        if not isinstance(tool,dict) or tool.get('type')!='function':return False
        if set(tool)-{'type','name','description','parameters','strict'}:return False
        if not isinstance(tool.get('name'),str) or not isinstance(tool.get('parameters'),dict):return False
    choice=request.get('tool_choice','auto')
    if isinstance(choice,dict):
        if set(choice)!={'type','name'} or choice.get('type')!='function' or not isinstance(choice.get('name'),str):return False
    elif choice not in ('auto','none','required'):return False
    def text_parts(content):
        if isinstance(content,str):return True
        return isinstance(content,list) and all(isinstance(p,dict) and p.get('type') in ('input_text','output_text')
            and isinstance(p.get('text'),str) and not set(p)-{'type','text','annotations','logprobs','prompt_cache_breakpoint'} for p in content)
    inputs=request.get('input')
    if isinstance(inputs,str):return True
    if not isinstance(inputs,list) or not inputs:return False
    for item in inputs:
        if not isinstance(item,dict):return False
        kind=item.get('type','message')
        if kind=='message':
            if set(item)-{'type','role','content','id','status'} or not text_parts(item.get('content')):return False
            if item.get('role') not in ('system','developer','user','assistant'):return False
        elif kind=='function_call':
            if set(item)-{'type','id','call_id','name','arguments','status'}:return False
            if not all(isinstance(item.get(k),str) for k in ('call_id','name','arguments')):return False
        elif kind=='function_call_output':
            if set(item)-{'type','id','call_id','output','status'}:return False
            if not isinstance(item.get('call_id'),str) or not text_parts(item.get('output')):return False
        elif kind=='reasoning':
            if set(item)-{'type','id','summary','encrypted_content','status'}:return False
            if not isinstance(item.get('encrypted_content'),str):return False
            if not isinstance(item.get('summary',[]),list):return False
            if any(not isinstance(p,dict) or p.get('type')!='summary_text' or not isinstance(p.get('text'),str) or set(p)-{'type','text'} for p in item.get('summary',[])):return False
        else:return False
    return True


def generation_bound_valid(call):
    """Verify the durable pre-send proof without treating unknown usage as zero."""
    if not isinstance(call,dict):return False
    proof=call.get('generation_bound') or {};request=call.get('request') or {}
    if not isinstance(proof,dict) or not isinstance(request,dict):return False
    count=call.get('input_tokens_preflight');cap=request.get('max_output_tokens');reserved=call.get('reserved_tokens')
    policy=proof.get('policy')
    compatible=(pure_text_generation(request) if policy=='pure-text-preflight-cap-v1' else
                client_function_generation(request) if policy=='client-function-preflight-cap-v2' else False)
    return (compatible
        and proof.get('provider')=='openai.responses' and proof.get('external_side_effects') is False
        and type(count) is int and count>=0 and type(cap) is int and cap>0
        and type(reserved) is int and reserved==count+cap
        and proof.get('input_tokens')==count and proof.get('max_output_tokens')==cap
        and proof.get('upper_bound_tokens')==reserved
        and proof.get('request_sha256')==_request_digest(request))


def generation_bound_proof(call):
    """Construct proof from an authoritative built-in transport reservation.

    Host code must establish native Responses provenance separately. A historical
    audit may call this on a COPY and persist a new receipt, never rewrite the
    original report or claim that its missing provider usage became exact.
    """
    if not isinstance(call,dict) or not pure_text_generation(call.get('request')):return None
    request=call['request']
    proof={'policy':'pure-text-preflight-cap-v1','provider':'openai.responses',
        'external_side_effects':False,'input_tokens':call.get('input_tokens_preflight'),
        'max_output_tokens':request.get('max_output_tokens'),'upper_bound_tokens':call.get('reserved_tokens'),
        'request_sha256':_request_digest(request)}
    return proof if generation_bound_valid({**call,'generation_bound':proof}) else None


def client_function_bound_proof(call):
    """Only the native client-function HTTP adapter may attest this provenance."""
    if not isinstance(call,dict) or not client_function_generation(call.get('request')):return None
    request=call['request']
    proof={'policy':'client-function-preflight-cap-v2','provider':'openai.responses',
        'external_side_effects':False,'input_tokens':call.get('input_tokens_preflight'),
        'max_output_tokens':request.get('max_output_tokens'),'upper_bound_tokens':call.get('reserved_tokens'),
        'request_sha256':_request_digest(request)}
    return proof if generation_bound_valid({**call,'generation_bound':proof}) else None


def embedding_bound_valid(call):
    """Count the actual token-array wire input; no tokenizer dependency here."""
    if not isinstance(call,dict):return False
    proof=call.get('embedding_bound');request=call.get('request');original=call.get('original_input')
    if not isinstance(proof,dict) or not isinstance(request,dict):return False
    if set(request)!={'model','input'} or request['model']!='text-embedding-ada-002':return False
    arrays=request['input']
    if not isinstance(arrays,list) or not arrays:return False
    if not all(isinstance(row,list) and row and all(type(token) is int and token>=0 for token in row) for row in arrays):return False
    if not isinstance(original,list) or len(original)!=len(arrays) or not all(isinstance(text,str) for text in original):return False
    count=sum(map(len,arrays));slack=call.get('reservation_slack_tokens');reserved=call.get('reserved_tokens')
    return (proof.get('policy')=='stateless-token-array-embedding-v1'
        and proof.get('provider')=='openai.embeddings' and proof.get('external_side_effects') is False
        and proof.get('encoding')=='cl100k_base' and call.get('input_encoding')=='cl100k_base'
        and type(slack) is int and slack>=0 and type(reserved) is int and reserved==count+slack
        and call.get('input_tokens_preflight')==count and proof.get('input_tokens')==count
        and proof.get('token_counts')==list(map(len,arrays))
        and proof.get('generated_output_tokens')==0 and proof.get('reservation_slack_tokens')==slack
        and proof.get('upper_bound_tokens')==reserved
        and proof.get('request_sha256')==_request_digest(request)
        and proof.get('original_input_sha256')==_request_digest(original))


def embedding_bound_proof(call):
    """Native embedding adapter supplies source text, encoding and actual arrays."""
    if not isinstance(call,dict):return None
    request=call.get('request')
    if not isinstance(request,dict) or not isinstance(request.get('input'),list):return None
    if not all(isinstance(row,list) for row in request['input']):return None
    proof={'policy':'stateless-token-array-embedding-v1','provider':'openai.embeddings',
        'external_side_effects':False,'encoding':'cl100k_base','generated_output_tokens':0,
        'input_tokens':sum(map(len,request['input'])),'token_counts':list(map(len,request['input'])),
        'reservation_slack_tokens':call.get('reservation_slack_tokens'),
        'upper_bound_tokens':call.get('reserved_tokens'),'request_sha256':_request_digest(request),
        'original_input_sha256':_request_digest(call.get('original_input'))}
    return proof if embedding_bound_valid({**call,'embedding_bound':proof}) else None


def budget_bounded(call):
    if call.get('accounting_error'):return False
    status=call.get('usage_status')
    if status in ('reported','not_sent'):return True
    return (status=='unknown_reserved_upper_bound' and (generation_bound_valid(call) or embedding_bound_valid(call))
            and call.get('charged_tokens')==call.get('reserved_tokens'))


def save_json(path, value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.'+str(uuid.uuid4())+'.tmp')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    # Windows readers and virus scanners may briefly deny replacement. Keep
    # the previous complete snapshot visible while retrying the same write.
    for attempt in range(9):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt==8:raise
            time.sleep(min(.01*2**attempt,.25))


class TokenBudgetExceeded(RuntimeError):
    def __init__(self,message,limit='run'):
        super().__init__(message)
        self.limit=limit


class RunRecorder:
    """One recorder per workflow run; adapters share this evidence contract.

    Provider integrations call before_model/after_model even for invalid output.
    Reservations cover missing usage on interrupted/failed provider requests.
    Audit computation never enters the provider integration or agent context.
    """
    def __init__(self,folder,run_id,workflow,token_budget=3_000_000,round_token_budget=1_000_000):
        if token_budget<=0 or (round_token_budget is not None and round_token_budget<=0):raise ValueError('Token budgets must be positive')
        self.folder=Path(folder);self.folder.mkdir(parents=True,exist_ok=True)
        self.run_id=run_id;self.workflow=workflow
        self.token_budget=token_budget;self.round_token_budget=round_token_budget
        self.round=None;self.rounds=[];self.sequence=0;self.models=[];self.env_calls=[]
        self.charged=0;self.known_tokens=0;self.unknown_usage=False
        self.budget_blocked=False;self.budget_limit=None;self.active_task=None;self.instances={}
        self.model_guard=None
        self._event_lock=threading.RLock()
        from runtime.source_guard import ACTIVE_GUARD
        self.runtime_guard=ACTIVE_GUARD.get()
        self.started_at=utc_now();self.started=time.perf_counter()

    def event(self,kind,**data):
        with self._event_lock:
            self.sequence+=1
            event={'schema_version':'1.0','sequence':self.sequence,'run_id':self.run_id,
                   'workflow':self.workflow,'round':self.round['round'] if self.round else None,
                   'time_utc':utc_now(),'kind':kind,**data}
            path=self.folder/'log/events.jsonl';path.parent.mkdir(parents=True,exist_ok=True)
            encoded=json.dumps(event,ensure_ascii=False)+'\n'
            # Retry only opening: after a write starts, a partial append must
            # never be guessed safe to repeat. Concurrent writers share a lock.
            for attempt in range(9):
                try:stream=path.open('a',encoding='utf-8');break
                except PermissionError:
                    if attempt==8:raise
                    time.sleep(min(.01*2**attempt,.25))
            with stream:
                stream.write(encoded);stream.flush()
            return event

    def begin_round(self):
        if self.round is not None:raise RuntimeError('Previous round is still open')
        if self.rounds and self.rounds[-1]['end_reason']=='round_token_budget_exhausted' and self.rounds[-1]['charged_tokens']==0:
            from .control import RunStopped
            raise RunStopped('round_input_exceeds_trial_cap','A fresh round could not fit even its first input; do not manufacture empty retries')
        if self.budget_limit=='round':
            self.budget_blocked=False;self.budget_limit=None
        self.round={'round':len(self.rounds)+1,'started_at':utc_now(),'steps':[],
                    'env_calls':[],'model_calls':[],'task_outcomes':[],
                    'initial_task':copy.deepcopy(self.active_task),'charged_tokens':0}
        self.round_started=time.perf_counter()
        self.event('round.start',initial_task=self.active_task)

    @property
    def remaining(self):
        run_remaining=self.token_budget-self.charged
        return min(run_remaining,self.round_token_budget-self.round['charged_tokens']) if self.round and self.round_token_budget is not None else run_remaining

    def before_model(self,payload,scope,input_tokens,*,reservation_slack_tokens=None):
        self.check_model_allowed()
        if self.budget_blocked:raise TokenBudgetExceeded('Token accounting blocked further provider calls',self.budget_limit)
        if not isinstance(input_tokens,int) or isinstance(input_tokens,bool) or input_tokens<0:
            raise ValueError('Provider input token count is required')
        remaining=self.remaining
        if reservation_slack_tokens is not None and (type(reservation_slack_tokens) is not int or reservation_slack_tokens<0):
            raise ValueError('Reservation slack must be a nonnegative integer')
        requested_output=payload['max_output_tokens'] if reservation_slack_tokens is None else reservation_slack_tokens
        limiting_budget=('round' if self.round and self.round_token_budget is not None
                         and self.round_token_budget-self.round['charged_tokens']<self.token_budget-self.charged else 'run')
        output_limit=min(requested_output,remaining-input_tokens)
        if output_limit<16:
            self.budget_blocked=True
            self.budget_limit='run' if self.token_budget-self.charged<input_tokens+16 else 'round'
            self.event('budget.blocked',scope=scope,input_tokens=input_tokens,remaining=remaining,limit=self.budget_limit)
            raise TokenBudgetExceeded('Insufficient remaining tokens for input and output',self.budget_limit)
        if reservation_slack_tokens is None:payload['max_output_tokens']=output_limit
        reserved=input_tokens+output_limit
        call={'call_id':str(uuid.uuid4()),'scope':scope,'input_tokens_preflight':input_tokens,
              'output_budget_clamped':output_limit<requested_output,'limiting_budget':limiting_budget,
              'reserved_tokens':reserved,'started_at':utc_now(),'request':copy.deepcopy(payload),
              '_started':time.perf_counter()}
        if reservation_slack_tokens is not None:
            call['reservation_slack_tokens']=output_limit
            call['reservation_kind']='input_plus_slack_without_generation_cap'
        self.charged+=reserved
        if self.round:
            self.round['charged_tokens']+=reserved;self.round['model_calls'].append(call)
        self.models.append(call)
        try:self.event('model.start',call={k:v for k,v in call.items() if k!='_started'})
        except BaseException as exc:
            # Transmission has not begun. Release this known-unsent reservation
            # even if the same locked ledger also rejects its cancellation log.
            try:self.cancel_model(call,{'type':type(exc).__name__,'message':str(exc)})
            except Exception as secondary:
                call['cancellation_log_error']={'type':type(secondary).__name__,'message':str(secondary)}
            raise
        return call

    def check_model_allowed(self):
        if self.runtime_guard is not None:self.runtime_guard()
        if self.model_guard is not None:self.model_guard()

    def raise_if_budget_truncated(self,call,response):
        """A budget-clamped incomplete response is an interruption, not a task failure."""
        if (isinstance(response,dict) and response.get('status')=='incomplete'
            and (response.get('incomplete_details') or {}).get('reason')=='max_output_tokens'
            and call.get('output_budget_clamped') and call.get('usage_status')=='reported'):
            self.budget_blocked=True;self.budget_limit=call['limiting_budget']
            self.event('budget.truncated_output',call_id=call['call_id'],limit=self.budget_limit)
            raise TokenBudgetExceeded('Model output reached the remaining token allocation',self.budget_limit)

    def before_send(self,call):
        self.check_model_allowed()

    def authorize_bounded_generation(self,call):
        """Called only by the built-in pure text transport before transmission.

        The full request is already in model.start. Persist the proof separately
        before sending; a logging failure remains a known-unsent cancellation.
        """
        proof=generation_bound_proof(call)
        if proof is None:return False
        call['generation_bound']=proof
        self.event('model.generation_bound',call_id=call['call_id'],generation_bound=call['generation_bound'])
        return True

    def authorize_bounded_client_functions(self,call):
        proof=client_function_bound_proof(call)
        if proof is None:return False
        call['generation_bound']=proof
        self.event('model.generation_bound',call_id=call['call_id'],generation_bound=proof)
        return True

    def authorize_bounded_embedding(self,call,*,original_input,encoding):
        """Native adapter attests tokenization; exact arrays determine the bound."""
        call['original_input']=copy.deepcopy(original_input);call['input_encoding']=encoding
        proof=embedding_bound_proof(call)
        if proof is None:return False
        call['embedding_bound']=proof
        self.event('model.embedding_bound',call_id=call['call_id'],embedding_bound=proof,
            original_input=call['original_input'],input_encoding=encoding)
        return True

    def cancel_model(self,call,error):
        """A guard blocked transmission; release the entire unsent reservation."""
        self.charged-=call['reserved_tokens']
        if self.round:self.round['charged_tokens']-=call['reserved_tokens']
        call.update(wall_seconds=time.perf_counter()-call.pop('_started'),finished_at=utc_now(),
                    usage_status='not_sent',charged_tokens=0,error=error,
                    usage={'input_tokens':0,'output_tokens':0,'total_tokens':0})
        self.event('model.not_sent',call=call)

    def after_model(self,call,response=None,error=None,*,usage_override=None):
        call['wall_seconds']=time.perf_counter()-call.pop('_started')
        call['finished_at']=utc_now();call['response']=copy.deepcopy(response)
        if error:call['error']=error
        usage=usage_override if usage_override is not None else (response.get('usage') if isinstance(response,dict) else None)
        valid=isinstance(usage,dict) and all(isinstance(usage.get(k),int) and not isinstance(usage.get(k),bool)
                                            and usage[k]>=0 for k in ('input_tokens','output_tokens','total_tokens'))
        valid=valid and usage['total_tokens']==usage['input_tokens']+usage['output_tokens']
        if valid:
            charged=usage['total_tokens'];self.known_tokens+=charged
            delta=charged-call['reserved_tokens'];self.charged+=delta
            if self.round:self.round['charged_tokens']+=delta
            call['usage']=usage;call['charged_tokens']=charged;call['usage_status']='reported'
            if charged>call['reserved_tokens']:
                self.budget_blocked=True;self.budget_limit='accounting';call['accounting_error']='Provider exceeded preflight reservation'
            elif generation_bound_valid(call) and (usage['input_tokens']>call['input_tokens_preflight']
                    or usage['output_tokens']>call['request']['max_output_tokens']):
                self.budget_blocked=True;self.budget_limit='accounting'
                call['accounting_error']='Provider usage violated the input-count/output-cap proof'
            elif embedding_bound_valid(call) and (usage['input_tokens']>call['input_tokens_preflight'] or usage['output_tokens']!=0):
                self.budget_blocked=True;self.budget_limit='accounting'
                call['accounting_error']='Provider usage violated the token-array embedding proof'
        else:
            self.unknown_usage=True
            call['usage']=None;call['charged_tokens']=call['reserved_tokens']
            call['usage_status']='unknown_reserved_upper_bound'
            if generation_bound_valid(call) or embedding_bound_valid(call):
                call['budget_accounting']='bounded_unknown'
            else:
                self.budget_blocked=True;self.budget_limit='accounting'
                call['budget_accounting']='unbounded_unknown'
        self.event('model.finish',call=call)

    def observe_env(self,request,invoke,stage='workflow'):
        from .environment_evidence import get_environment_feedback
        call={'call_id':str(uuid.uuid4()),'stage':stage,'started_at':utc_now(),'request':copy.deepcopy(request),
              'task_at_start':copy.deepcopy(self.active_task)}
        self.event('env.start',call=call)
        started=time.perf_counter()
        interruption=None
        try:
            feedback=invoke(request)
        except BaseException as exc:
            is_interruption=not isinstance(exc,Exception) or isinstance(exc,TokenBudgetExceeded)
            feedback=get_environment_feedback(exc,request)
            if feedback is None:
                feedback={'protocol_version':'1.0','request_id':request.get('request_id','invalid'),
                          'session_id':'unavailable','status':'pending_or_unknown' if is_interruption else 'error','result':None,
                          'error':{'type':type(exc).__name__,'message':str(exc)}}
                if is_interruption:feedback['execution']={'state':'unknown','retry':'observe_before_new_request',
                    'next_steps':['The interrupted operation has no bound canonical feedback. Inspect its original request and actual state before retrying.']}
            call['raised_exception']=True
            if is_interruption:interruption=exc
        call.update(finished_at=utc_now(),wall_seconds=time.perf_counter()-started,feedback=copy.deepcopy(feedback))
        self.env_calls.append(call)
        if self.round:self.round['env_calls'].append(call)
        self._task_evidence(call)
        self.event('env.finish',call=call)
        if interruption is not None:
            interruption.recorded_environment_call=call
            raise interruption
        return feedback,call

    def _task_evidence(self,call):
        request=call['request'];feedback=call['feedback'];value=feedback.get('result')
        if request.get('op')=='task.start' and feedback.get('status')=='completed' and isinstance(value,dict):
            self.active_task={'task_id':value['task']['id'],'task_instance_id':value['id'],
                              'profile_id':value['task'].get('selected_profile')}
            self.instances[value['id']]=copy.deepcopy(value)
            call['bound_task']=copy.deepcopy(self.active_task)
        if request.get('op')!='evaluation.run' or self.round is None:return
        params=request.get('params')
        params=params if isinstance(params,dict) else {}
        submission=params.get('submission')
        submission=submission if isinstance(submission,dict) else {}
        iid=(value or {}).get('task_instance_id',submission.get('task_instance_id')) if isinstance(value,dict) else submission.get('task_instance_id')
        instance=self.instances.get(iid,{}) if isinstance(iid,str) else {}
        verdict=value.get('verdict') if feedback.get('status')=='completed' and isinstance(value,dict) else None
        self.round['task_outcomes'].append({'call_id':call['call_id'],'task_instance_id':iid,
            'task_id':value.get('task_id') if isinstance(value,dict) and value.get('task_id') else instance.get('task',{}).get('id'),
            'evaluation_id':value.get('evaluation_id') if isinstance(value,dict) else None,
            'verdict':verdict,'success':True if verdict=='pass' else False if verdict=='fail' else None,
            'status':verdict or feedback.get('status'),'op':request['op']})

    def finish_round(self,reason):
        round_record=self.round
        round_record.update(finished_at=utc_now(),wall_seconds=time.perf_counter()-self.round_started,
                            end_reason=reason,final_task=copy.deepcopy(self.active_task))
        round_record['env_wall_seconds']=sum(c['wall_seconds'] for c in round_record['env_calls'])
        round_record['model_wall_seconds']=sum(c['wall_seconds'] for c in round_record['model_calls'])
        round_record['token_usage']=summarize_usage(round_record['model_calls'])
        if not round_record['task_outcomes']:
            round_record['task_outcomes']=[{**(self.active_task or {}),'verdict':None,'success':None,'status':'not_evaluated'}]
        self.rounds.append(round_record)
        self.round=None
        # Keep the completed round in memory for the final authoritative report
        # even if its append-only event or per-round projection is unavailable.
        self.event('round.finish',round=round_record['round'],summary={k:v for k,v in round_record.items() if k not in ('steps','env_calls','model_calls')})
        save_json(self.folder/f"rounds/{round_record['round']:04d}.json",round_record)
        return round_record


def summarize_usage(calls):
    totals={k:0 for k in ('input_tokens','output_tokens','total_tokens','cached_input_tokens','cache_write_tokens','cache_write_30m_tokens','cache_write_12h_tokens','reasoning_tokens')}
    for call in calls:
        usage=call.get('usage') or {}
        for key in ('input_tokens','output_tokens','total_tokens'):totals[key]+=usage.get(key,0)
        totals['cached_input_tokens']+=usage.get('input_tokens_details',{}).get('cached_tokens',0)
        totals['cache_write_tokens']+=usage.get('input_tokens_details',{}).get('cache_write_tokens',0)
        split=usage.get('input_tokens_details',{}).get('cache_write_tokens_details') or {}
        totals['cache_write_30m_tokens']+=split.get('ttl_30m_tokens',0)
        totals['cache_write_12h_tokens']+=split.get('ttl_12h_tokens',0)
        totals['reasoning_tokens']+=usage.get('output_tokens_details',{}).get('reasoning_tokens',0)
    by_scope={}
    for call in calls:
        scope=by_scope.setdefault(call['scope'],{'calls':0,'input_tokens':0,'output_tokens':0,'total_tokens':0,'charged_tokens':0,'cached_input_tokens':0,'cache_write_tokens':0})
        scope['calls']+=1
        for key in ('input_tokens','output_tokens','total_tokens'):scope[key]+=(call.get('usage') or {}).get(key,0)
        scope['charged_tokens']+=call.get('charged_tokens',0)
        scope['cached_input_tokens']+=(call.get('usage') or {}).get('input_tokens_details',{}).get('cached_tokens',0)
        scope['cache_write_tokens']+=(call.get('usage') or {}).get('input_tokens_details',{}).get('cache_write_tokens',0)
    from .pricing import estimate
    return {**totals,'charged_tokens':sum(c.get('charged_tokens',0) for c in calls),'by_scope':by_scope,
            'ordinary_input_tokens':totals['input_tokens']-totals['cached_input_tokens']-totals['cache_write_tokens'],
            'cache_write_lifetime_unreported_tokens':totals['cache_write_tokens']-totals['cache_write_30m_tokens']-totals['cache_write_12h_tokens'],
            'cache_hit_ratio':totals['cached_input_tokens']/totals['input_tokens'] if totals['input_tokens'] else 0,
            'cost_estimate':estimate(calls),
            'complete':all(c.get('usage_status') in ('reported','not_sent') for c in calls),
            'budget_bounded_complete':all(budget_bounded(c) for c in calls),
            'unknown_usage_upper_bound_tokens':sum(c.get('charged_tokens',0) for c in calls
                if c.get('usage_status')=='unknown_reserved_upper_bound'),
            'bounded_unknown_calls':sum(c.get('usage_status')=='unknown_reserved_upper_bound' and budget_bounded(c) for c in calls),
            'unbounded_unknown_calls':sum(c.get('usage_status')=='unknown_reserved_upper_bound' and not budget_bounded(c) for c in calls)}
