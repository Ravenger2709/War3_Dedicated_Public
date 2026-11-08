// SPDX-License-Identifier: Apache-2.0
// Original, probe-only experiment. This class cannot generate actor commands.
#ifndef WAR3_NATIVE_G1_DUAL_PROBE_H
#define WAR3_NATIVE_G1_DUAL_PROBE_H
#include "native_g1_bridge.h"
#include <algorithm>
namespace war3g1 {
struct DualProbeResult {
 bool allowed,queued,peer_ack,confirmed,failed,diagnostic;
 uint32_t pid0,pid1,raw0,raw1,accepted;
 Bytes downlink0,downlink1;
 DualProbeResult():allowed(true),queued(false),peer_ack(false),confirmed(false),failed(false),diagnostic(false),pid0(255),pid1(255),raw0(0),raw1(0),accepted(0){}
};
class DualProbeSession {
 uint32_t a_[2],b_[2],epoch_,pid_[2],tick_[2],probe_tick_;
 uint32_t raw0_[2],raw1_[2],accepted_[2],fields_[2];
 bool enabled_,retired_,sent_,confirmed_,ack_[2];
 void fail(DualProbeResult &r) {
  retired_=true;confirmed_=false;r.failed=true;r.queued=false;r.confirmed=false;r.peer_ack=false;r.diagnostic=false;
  r.downlink0.clear();r.downlink1.clear();
 }
 Bytes packet(unsigned peer)const {
  std::ostringstream s;s<<"~g1:"<<epoch_<<":0:"<<probe_tick_<<":0";
  std::string text=s.str();Bytes out(1,0x60);put32(out,a_[peer]);put32(out,b_[peer]);
  out.insert(out.end(),text.begin(),text.end());out.push_back(0);return out;
 }
public:
 DualProbeSession():epoch_(0),probe_tick_(0),enabled_(false),retired_(false),sent_(false),confirmed_(false) {
  for(unsigned i=0;i<2;++i){a_[i]=b_[i]=tick_[i]=raw0_[i]=raw1_[i]=accepted_[i]=fields_[i]=0;pid_[i]=255;ack_[i]=false;}
 }
 void configure(uint32_t a0,uint32_t b0,uint32_t a1,uint32_t b1,uint32_t epoch) {
  *this=DualProbeSession();a_[0]=a0;b_[0]=b0;a_[1]=a1;b_[1]=b1;epoch_=epoch;
  enabled_=epoch>0 && epoch<(1U<<28) && a0 && b0 && a1 && b1 &&
   a0<=65535 && b0<=65535 && a1<=65535 && b1<=65535;
 }
 bool enabled()const{return enabled_;}
 void depart(uint32_t pid){if(pid==pid_[0] || pid==pid_[1])retired_=true;}
 DualProbeResult process(uint32_t pid,unsigned colour,const Bytes &v,bool running) {
  DualProbeResult r;if(!enabled_)return r;
  std::vector<Event> events;
  if(!decode(v,events)){r.allowed=false;return r;}
  for(size_t i=0;i<events.size();++i)
   if(events[i].opcode==0x60 && events[i].text.compare(0,4,"~g1:")==0){r.allowed=false;return r;}
  if(!running || retired_ || colour>1 || pid<1 || pid>254)return r;
  // Fence the entire batch before an earlier ACK could use an old clock.
  if(sent_ && !confirmed_ && pid_[colour]==pid)for(size_t i=0;i<events.size();++i){
   const Event &e=events[i];
   if(e.opcode==0x6b && e.cache=="AIG1.w3v" && e.mission=="g1" &&
      e.key=="tick" && e.value>probe_tick_+20){fail(r);return r;}
  }
  for(size_t i=0;i<events.size();++i){
   const Event &e=events[i];
   if(e.opcode!=0x6b || e.cache!="AIG1.w3v" || e.mission!="g1")continue;
   if(e.key=="wire_version" && e.value==3){
    if((pid_[colour]!=255 && pid_[colour]!=pid) || pid_[1-colour]==pid){fail(r);return r;}
    pid_[colour]=pid;
   }
   if(pid_[colour]!=pid)continue;
   if(e.key=="tick"){
    if(e.value>10000000){fail(r);return r;}
    if(e.value>tick_[colour])tick_[colour]=e.value;
    if(!sent_ && pid_[0]!=255 && pid_[1]!=255 && tick_[0]>=8 && tick_[1]>=8 &&
       std::max(tick_[0],tick_[1])-std::min(tick_[0],tick_[1])<=20){
     sent_=true;probe_tick_=std::max(tick_[0],tick_[1]);
     r.queued=true;r.pid0=pid_[0];r.pid1=pid_[1];r.downlink0=packet(0);r.downlink1=packet(1);
     fields_[0]=fields_[1]=0;
    }
    if(sent_ && !confirmed_ && tick_[colour]>probe_tick_+20){fail(r);return r;}
   }
   if(!sent_)continue;
   if(e.key=="raw0" || e.key=="raw1" || e.key=="accepted_probe"){
    if(e.value>1){fail(r);return r;}
    if((e.key=="raw0" && (fields_[colour]&1) && e.value<raw0_[colour]) ||
       (e.key=="raw1" && (fields_[colour]&2) && e.value<raw1_[colour]) ||
       (e.key=="accepted_probe" && (fields_[colour]&4) && e.value<accepted_[colour])){fail(r);return r;}
    if(e.key=="raw0"){raw0_[colour]=e.value;fields_[colour]|=1;}
    if(e.key=="raw1"){raw1_[colour]=e.value;fields_[colour]|=2;}
    if(e.key=="accepted_probe"){accepted_[colour]=e.value;fields_[colour]|=4;}
    if(fields_[colour]==7){r.diagnostic=true;r.raw0=raw0_[colour];r.raw1=raw1_[colour];r.accepted=accepted_[colour];}
   }
   if(e.key=="probe" && e.value==epoch_ && !ack_[colour] && tick_[colour]>probe_tick_ && tick_[colour]<=probe_tick_+20){
    if(fields_[colour]!=7 || accepted_[colour]!=1 || raw0_[colour]+raw1_[colour]<1){fail(r);return r;}
    ack_[colour]=true;r.peer_ack=true;
    if(ack_[0] && ack_[1] && !confirmed_){confirmed_=true;r.confirmed=true;}
   }
  }
  return r;
 }
};
}
#endif
