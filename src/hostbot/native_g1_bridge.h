// SPDX-License-Identifier: Apache-2.0
// Original, deliberately restricted Warcraft III 1.26a G1 experiment.
#ifndef WAR3_NATIVE_G1_BRIDGE_H
#define WAR3_NATIVE_G1_BRIDGE_H
#include <stdint.h>
#include <string>
#include <vector>
#include <set>
#include <map>
#include <sstream>
namespace war3g1 {
typedef std::vector<unsigned char> Bytes;
struct Event { unsigned opcode; uint32_t a,b,value; std::string cache,mission,key,text; Bytes wire;
 Event(unsigned op=0):opcode(op),a(0),b(0),value(0){} };
inline uint32_t u32(const Bytes &v,size_t p) {
 return uint32_t(v[p])|(uint32_t(v[p+1])<<8)|(uint32_t(v[p+2])<<16)|(uint32_t(v[p+3])<<24);
}
inline bool take(const Bytes &v,size_t &p,size_t n) {
 if(p>v.size() || n>v.size()-p)return false; p+=n;return true;
}
inline bool str(const Bytes &v,size_t &p,std::string &out) {
 size_t begin=p;while(p<v.size() && v[p] && p-begin<=128)++p;
 if(p==v.size() || p-begin>128)return false;
 out.assign(v.begin()+begin,v.begin()+p);++p;return true;
}
// Decode at known action boundaries. Never scan for opcodes inside payloads.
// Unknown opcodes are NOT accepted in active G1, not a generic replay decoder.
inline bool decode(const Bytes &v,std::vector<Event> &events) {
 events.clear(); if(v.size()>1027)return false; size_t p=0;
 while(p<v.size()) {
  size_t begin=p;unsigned op=v[p++]; size_t n=0; Event e(op);
  switch(op) {
   case 0x01:case 0x03:n=1;break;
   case 0x02:case 0x04:case 0x05:case 0x1a:case 0x61:case 0x66:case 0x67:break;
   case 0x07:n=4;break;
   case 0x10:n=14;break;case 0x11:n=22;break;case 0x12:n=30;break;
   case 0x13:n=38;break;case 0x14:n=43;break;case 0x15:n=51;break;
   case 0x16:case 0x17:
    if(p+3>v.size())return false;
    n=3+8*(unsigned(v[p+1])+(unsigned(v[p+2])<<8));break;
   case 0x18:n=2;break;case 0x19:n=12;break;
   case 0x1b:case 0x1c:n=9;break;case 0x1d:n=8;break;
   case 0x1e:case 0x1f:n=5;break;case 0x50:n=5;break;case 0x51:n=9;break;
   case 0x62:n=12;break;case 0x63:n=8;break;case 0x64:case 0x65:n=8;break;
   case 0x06:
    if(!str(v,p,e.text)||!str(v,p,e.key)||!take(v,p,1))return false;break;
   case 0x60:
    if(p+8>v.size())return false;
    e.a=u32(v,p);e.b=u32(v,p+4);p+=8;
    if(!str(v,p,e.text))return false;e.wire.assign(v.begin()+begin,v.begin()+p);events.push_back(e);break;
   case 0x6b:case 0x6c:case 0x6d:
    if(!str(v,p,e.cache)||!str(v,p,e.mission)||!str(v,p,e.key)||p+4>v.size())return false;
    e.value=u32(v,p);p+=4;events.push_back(e);break;
   default:return false;
  }
  if(!take(v,p,n))return false;
 }
 return true;
}
inline bool decimal(const std::string &s,uint32_t &out) {
 if(s.empty() || s.size()>10)return false; uint64_t n=0;
 for(size_t i=0;i<s.size();++i){if(s[i]<'0'||s[i]>'9')return false;n=n*10+s[i]-'0';if(n>0xffffffffULL)return false;}
 out=static_cast<uint32_t>(n);return true;
}
// Experimental coalescing: keep the publisher's original and generated actions
// in one bounded PID record. Packet CRC is recalculated by native aggregation.
inline bool append_downlink(Bytes &original,const Bytes &downlink) {
 if(original.size()>1024 || downlink.size()>1024-original.size())return false;
 original.insert(original.end(),downlink.begin(),downlink.end());return true;
}
inline void put32(Bytes &v,uint32_t n) {for(unsigned i=0;i<4;++i)v.push_back((n>>(i*8))&255);}
struct Result {
 bool allowed,calibration,ack,snapshot,bound,probe_ready,probe_failed,probe_packet;uint32_t receiver_a,receiver_b,ack_sequence,digest,meta0,meta11,target;
 Bytes downlink,calibration_action;
 Result():allowed(true),calibration(false),ack(false),snapshot(false),bound(false),probe_ready(false),probe_failed(false),probe_packet(false),receiver_a(0),receiver_b(0),ack_sequence(0),digest(0),meta0(0),meta11(0),target(0){}
};
class Session {
 unsigned mode_;uint32_t a_,b_,epoch_,publisher_,tick_,seq_,pending_,last_sent_tick_;
 uint32_t protocol_,meta0_,meta11_,target_,probe_tick_;
 bool bound_,probe_sent_,verified_;
 bool retired_,calibrated_,publisher_ack_;std::set<uint32_t> acknowledgers_;std::map<uint32_t,uint32_t> digests_;
public:
 Session():mode_(0),a_(0),b_(0),epoch_(0),publisher_(255),tick_(0),seq_(0),pending_(0),last_sent_tick_(0),protocol_(0),meta0_(0),meta11_(0),target_(0),probe_tick_(0),bound_(false),probe_sent_(false),verified_(false),retired_(false),calibrated_(false),publisher_ack_(false){}
 void configure(unsigned mode,uint32_t a,uint32_t b,uint32_t epoch) {
  *this=Session(); mode_=mode;a_=a;b_=b;epoch_=epoch;
 }
 bool enabled()const{return mode_!=0;}
 bool active()const{return (mode_==2 || mode_==4) && !retired_;}
 void depart(uint32_t pid) {if(pid==publisher_){retired_=true;pending_=0;}}
 Result process(uint32_t pid,unsigned colour,const Bytes &v,bool running) {
  Result r;if(!enabled())return r;
  std::vector<Event> events;
  if(!decode(v,events)){if(mode_>=2)r.allowed=false;return r;}
  // Validate whole action batch before any observation mutates session state.
  if(mode_>=2)for(size_t i=0;i<events.size();++i)
   if(events[i].opcode==0x60 && events[i].text.compare(0,4,"~g1:")==0){r.allowed=false;return r;}
  if(!running || retired_)return r;
  // A late publisher clock anywhere in this batch invalidates the outstanding
  // probe before an earlier ACK can grant authority using the previous clock.
  if(mode_==4 && probe_sent_ && !verified_ && pid==publisher_ && colour==0)
   for(size_t i=0;i<events.size();++i) {
    const Event &e=events[i];
    if(e.opcode==0x6b && e.cache=="AIG1.w3v" && e.mission=="g1" && e.key=="tick" &&
       e.value>tick_ && e.value<=10000000 && e.value>probe_tick_+20) {
     retired_=true;r.probe_failed=true;return r;
    }
   }
  for(size_t i=0;i<events.size();++i) {
   const Event &e=events[i];
   if(e.opcode==0x60 && (colour==0 || (mode_==1 && colour<12)) && e.text=="g1:hello") {
    r.calibration=true;r.receiver_a=e.a;r.receiver_b=e.b;r.calibration_action=e.wire;
    if(mode_==2 && colour==0 && active() && e.a==a_ && e.b==b_ && publisher_==255){publisher_=pid;calibrated_=true;}
   }
   if(mode_==4 && e.opcode==0x6b && e.cache=="AIG1.w3v" && e.mission=="g1" && colour==0) {
    // Only a wire pair captured from the actual sender's chat is configured.
    // JASS GetHandleId values are NOT wire object identifiers (real-client test).
    // Rehosting can change a captured pair; require a new harmless probe ACK.
    if(e.key=="wire_version" && e.value==2 && publisher_==255){
     publisher_=pid;protocol_=2;target_=a_;bound_=calibrated_=true;r.bound=true;r.target=target_;
    }
    if(e.key=="probe" && pid==publisher_ && probe_sent_ && !verified_ && e.value==epoch_ && !r.probe_packet && tick_>probe_tick_ && tick_<=probe_tick_+20) {
     verified_=true;r.probe_ready=true;
    }
   }
   if(!active()||!calibrated_||e.opcode!=0x6b||e.cache!="AIG1.w3v"||e.mission!="g1")continue;
   if(e.key=="digest") {digests_[pid]=e.value;continue;}
   if(e.key=="ack" && pending_ && e.value==pending_ && digests_.count(pid) && acknowledgers_.insert(pid).second) {
    r.ack=true;r.ack_sequence=e.value;r.digest=digests_[pid];
    // Record all peer ACKs until next command; publisher ACK releases backpressure.
    if(pid==publisher_)publisher_ack_=true;
   }
   if(pid!=publisher_ || colour!=0 || e.key!="tick" || e.value<=tick_ || e.value>10000000)continue;
   tick_=e.value;r.snapshot=true;
   if(mode_==4 && !verified_) {
    if(probe_sent_ && tick_>probe_tick_+20){retired_=true;r.probe_failed=true;return r;}
    if(!probe_sent_ && tick_>=8) {
     probe_sent_=true;r.probe_packet=true;probe_tick_=last_sent_tick_=tick_;
     std::ostringstream probe;probe<<"~g1:"<<epoch_<<":0:"<<tick_<<":0";
     std::string text=probe.str();r.downlink.push_back(0x60);put32(r.downlink,a_);put32(r.downlink,b_);
     r.downlink.insert(r.downlink.end(),text.begin(),text.end());r.downlink.push_back(0);
    }
    continue;
   }
   // At most one command per two simulated seconds; no wall-clock retries.
   if(tick_<last_sent_tick_+8 || !r.downlink.empty())continue;
   if(pending_ && !publisher_ack_ && tick_<last_sent_tick_+20)continue;
   last_sent_tick_=tick_;pending_=++seq_;publisher_ack_=false;acknowledgers_.clear();digests_.clear();
   std::ostringstream s;s<<"~g1:"<<epoch_<<":"<<seq_<<":"<<tick_<<":"<<(seq_%2 ? 1 : 2);
   std::string text=s.str();r.downlink.push_back(0x60);put32(r.downlink,a_);put32(r.downlink,b_);
   r.downlink.insert(r.downlink.end(),text.begin(),text.end());r.downlink.push_back(0);
  }
  return r;
 }
};
}
#endif
