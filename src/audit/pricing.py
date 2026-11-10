"""Versioned public-rate estimates, never an account invoice or network call."""
from decimal import Decimal as D
import json

PRICING={'version':'gpt-6.1-sol-standard-2026-11-05','model':'gpt-6.1-sol','currency':'USD',
         'rates_per_million':{'ordinary_input':'2','cached_input':'0.10','write_30m':'2.50','write_12h':'5','output':'10'},
         'source':'https://developers.openai.com/api/docs/models/gpt-6.1-sol',
         'cache_source':'https://developers.openai.com/api/docs/guides/prompt-caching',
         'long_context_threshold':272000,'long_context_input_multiplier':2,'long_context_output_multiplier':'1.5',
         'basis':'Published Standard rates; excludes regional/account-specific adjustments; not an invoice'}


def estimate(calls):
    known=D(0);unknown_upper=D(0);unpriced=[];unknown=0
    for call in calls:
        if call.get('usage_status')=='not_sent':continue
        request=call.get('request',{});response=call.get('response') or {}
        if request.get('model')!=PRICING['model'] or response.get('service_tier',request.get('service_tier','default')) not in ('default','standard','auto',None):
            unpriced.append({'call_id':call.get('call_id'),'reason':'Unsupported model or service tier'});continue
        usage=call.get('usage')
        if usage is None:
            unknown+=1;n=call.get('input_tokens_preflight');output=request.get('max_output_tokens')
            if type(n) is not int or type(output) is not int:
                unpriced.append({'call_id':call.get('call_id'),'reason':'Unknown reservation'});continue
            # Any potentially selected write may cover a prefix. Use the more
            # expensive requested lifetime; implicit old requests default 30m.
            ttl=request.get('prompt_cache_options',{}).get('ttl','30m')
            twelve=ttl=='12h' or '"ttl": "12h"' in json.dumps(request.get('input',[]))
            i_mult=2 if n>272000 else 1;o_mult=D('1.5') if n>272000 else D(1)
            unknown_upper+=(D(n)*(D(5) if twelve else D('2.5'))*i_mult+D(output)*10*o_mult)/1000000
            continue
        details=usage.get('input_tokens_details') or {};n=usage['input_tokens'];cached=details.get('cached_tokens',0);writes=details.get('cache_write_tokens',0)
        split=details.get('cache_write_tokens_details')
        if split is None:
            # Valid for our single-lifetime transport and pre-change implicit
            # 30m records. Never guess the split for explicitly mixed lifetimes.
            ttl=request.get('prompt_cache_options',{}).get('ttl','30m')
            explicit_ttls={block.get('prompt_cache_breakpoint',{}).get('ttl',ttl)
                           for item in request.get('input',[]) if isinstance(item,dict) and isinstance(item.get('content'),list)
                           for block in item['content'] if isinstance(block,dict) and 'prompt_cache_breakpoint' in block}
            if len(explicit_ttls|{ttl})>1:
                unpriced.append({'call_id':call.get('call_id'),'reason':'Mixed cache lifetime split unavailable'});continue
            w30,w12=(0,writes) if ttl=='12h' else (writes,0)
        else:w30,w12=split.get('ttl_30m_tokens',0),split.get('ttl_12h_tokens',0)
        if any(type(x) is not int or x<0 for x in (cached,writes,w30,w12)) or cached+writes>n or w30+w12!=writes:
            unpriced.append({'call_id':call.get('call_id'),'reason':'Inconsistent input category counts'});continue
        i_mult=2 if n>272000 else 1;o_mult=D('1.5') if n>272000 else D(1)
        known+=((D(n-cached-writes)*2+D(cached)*D('.1')+D(w30)*D('2.5')+D(w12)*5)*i_mult+D(usage['output_tokens'])*10*o_mult)/1000000
    return {'pricing':PRICING,'reported_usage_usd':str(known),'unknown_calls':unknown,
            'unknown_reservations_upper_usd':str(unknown_upper),'minimum_usd':str(known),
            'maximum_usd':str(known+unknown_upper) if not unpriced else None,'unpriced_calls':unpriced,
            'complete':not unknown and not unpriced}
