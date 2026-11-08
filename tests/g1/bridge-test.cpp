#include "src/hostbot/native_g1_bridge.h"
#include <cassert>
#include <iostream>
using namespace war3g1;
Bytes chat(const std::string &s,uint32_t a=17,uint32_t b=23){Bytes v(1,0x60);put32(v,a);put32(v,b);v.insert(v.end(),s.begin(),s.end());v.push_back(0);return v;}
Bytes cache(const std::string &key,uint32_t val){Bytes v(1,0x6b);const char *fields[]={"AIG1.w3v","g1",key.c_str()};for(unsigned i=0;i<3;++i){std::string s(fields[i]);v.insert(v.end(),s.begin(),s.end());v.push_back(0);}put32(v,val);return v;}
int main(){
 uint32_t number=0;assert(decimal("4294967295",number)&&number==0xffffffffU);for(const char *bad:{"","-1","1x","4294967296"," 2"})assert(!decimal(bad,number));
 Session s;assert(s.process(1,0,chat("~g1:forged"),true).allowed);s.configure(2,17,23,9);
 assert(s.process(1,0,cache("tick",8),true).downlink.empty());
 assert(!s.process(1,0,chat("~g1:9:1:8:1"),true).allowed);
 Bytes bad=chat("g1:hello");bad.push_back(0xff);assert(!s.process(1,0,bad,true).allowed);
 assert(s.process(1,0,cache("tick",8),true).downlink.empty());
 s.process(2,1,chat("g1:hello"),true);assert(s.process(2,1,cache("tick",8),true).downlink.empty());
 s.process(1,0,chat("g1:hello",18),true);assert(s.process(1,0,cache("tick",8),true).downlink.empty());
 s.process(1,0,chat("g1:hello"),true);Result r=s.process(1,0,cache("tick",8),true);assert(r.downlink==chat("~g1:9:1:8:1"));
 assert(!s.process(1,0,cache("ack",8),true).ack);s.process(1,0,cache("digest",42),true);assert(s.process(1,0,cache("ack",1),true).ack);
 assert(!s.process(1,0,cache("ack",1),true).ack);s.process(2,1,cache("digest",42),true);assert(s.process(2,1,cache("ack",1),true).ack);assert(s.process(1,0,cache("tick",9),true).downlink.empty());
 assert(s.process(1,0,cache("tick",16),true).downlink==chat("~g1:9:2:16:2"));
 Session other;other.configure(2,17,23,10);assert(other.process(1,0,cache("ack",2),true).downlink.empty());
 assert(!s.process(2,1,chat("~g1:forged"),false).allowed);
 s.depart(2);assert(s.active());s.depart(1);assert(!s.active());assert(!s.process(2,1,chat("~g1:forged"),true).allowed);assert(s.process(1,0,cache("tick",40),true).downlink.empty());
 for(size_t length=1;length<chat("g1:hello").size();++length){Bytes v=chat("g1:hello");v.resize(length);std::vector<Event> e;assert(!decode(v,e));}
 for(size_t length=1;length<cache("tick",8).size();++length){Bytes v=cache("tick",8);v.resize(length);std::vector<Event> e;assert(!decode(v,e));}
 // Bytes resembling a command inside an order payload are never interpreted as chat.
 Bytes order(23,0);order[0]=0x11;std::string marker="~g1:";std::copy(marker.begin(),marker.end(),order.begin()+2);
 Session t;t.configure(2,17,23,1);assert(t.process(1,0,order,true).allowed);
 Bytes combined=order, hello=chat("g1:hello");combined.insert(combined.end(),hello.begin(),hello.end());assert(t.process(1,0,combined,true).calibration);
 // One outstanding command, expiry allows progress; duplicate/future ACKs cannot release it.
 Session lease;lease.configure(2,17,23,4);lease.process(1,0,chat("g1:hello"),true);
 assert(!lease.process(1,0,cache("tick",8),true).downlink.empty());
 assert(lease.process(1,0,cache("tick",16),true).downlink.empty());
 assert(lease.process(2,1,cache("tick",28),true).downlink.empty());
 assert(!lease.process(1,0,cache("tick",28),true).downlink.empty());
 assert(lease.process(1,0,cache("tick",28),true).downlink.empty());
 assert(!lease.process(1,0,cache("ack",1),true).ack);
 Session calibration;calibration.configure(1,0,0,1);Bytes unknown(1,255);
 assert(calibration.process(1,0,unknown,true).allowed);
 assert(calibration.process(1,0,cache("tick",8),true).downlink.empty());
 Bytes original=cache("tick",8), cmd=chat("~g1:1:1:8:1"), want=original;
 want.insert(want.end(),cmd.begin(),cmd.end());assert(append_downlink(original,cmd));assert(original==want);
 Bytes full(1024,1), before=full;assert(!append_downlink(full,cmd));assert(full==before);
 // A genuinely captured sender pair is still unverified after rehosting until probe ACK.
 Session binding;binding.configure(4,832,832,9);
 Bytes meta=cache("wire_version",2), m0=cache("receiver0",0x100335), m11=cache("receiver11",0x100340);
 meta.insert(meta.end(),m0.begin(),m0.end());meta.insert(meta.end(),m11.begin(),m11.end());
 Result candidate=binding.process(1,0,meta,true);assert(candidate.bound && candidate.target==832);
 Result probe=binding.process(1,0,cache("tick",8),true);
 assert(probe.probe_packet && probe.downlink==chat("~g1:9:0:8:0",832,832));
 assert(!binding.process(1,0,cache("probe",9),true).probe_ready); // clock has not advanced
 assert(binding.process(1,0,cache("tick",9),true).downlink.empty());
 assert(!binding.process(2,1,cache("probe",9),true).probe_ready);
 assert(!binding.process(1,0,cache("probe",8),true).probe_ready);
 assert(binding.process(1,0,cache("probe",9),true).probe_ready);
 assert(binding.process(1,0,cache("tick",16),true).downlink==chat("~g1:9:1:16:1",832,832));
 Session timeout;timeout.configure(4,832,832,9);timeout.process(1,0,meta,true);
 timeout.process(1,0,cache("tick",8),true);assert(timeout.process(1,0,cache("tick",29),true).probe_failed);
 assert(timeout.process(1,0,cache("tick",40),true).downlink.empty());
 Session capture11;capture11.configure(1,0,0,9);assert(capture11.process(7,11,chat("g1:hello",832,832),true).calibration);
 Session active11;active11.configure(2,832,832,9);assert(active11.process(7,11,chat("g1:hello",832,832),true).downlink.empty());assert(active11.process(7,11,cache("tick",8),true).downlink.empty());
 Session early;early.configure(4,832,832,9);early.process(1,0,meta,true);
 Bytes forged=cache("tick",8), early_ack=cache("probe",9), later_tick=cache("tick",9);
 forged.insert(forged.end(),early_ack.begin(),early_ack.end());forged.insert(forged.end(),later_tick.begin(),later_tick.end());
 Result premature=early.process(1,0,forged,true);assert(premature.probe_packet && !premature.probe_ready);
 // Probe expiry is checked against the whole batch, even with ACK before tick.
 for(unsigned reverse=0;reverse<2;++reverse) {
  Session stale;stale.configure(4,832,832,9);stale.process(1,0,meta,true);
  stale.process(1,0,cache("tick",8),true);stale.process(1,0,cache("tick",9),true);
  Bytes ack_first=cache(reverse?"tick":"probe",reverse?29:9);
  Bytes tail=cache(reverse?"probe":"tick",reverse?9:29);
  ack_first.insert(ack_first.end(),tail.begin(),tail.end());
  Result expired=stale.process(1,0,ack_first,true);
  assert(expired.probe_failed && !expired.probe_ready && expired.downlink.empty());
  assert(!stale.active() && stale.process(1,0,cache("tick",40),true).downlink.empty());
 }
 // A simulation pause or ignored lag observation cannot advance authority/time.
 Session paused;paused.configure(4,832,832,20);paused.process(1,0,meta,true);
 assert(paused.process(1,0,cache("tick",8),true).probe_packet);
 assert(!paused.process(1,0,cache("tick",100),false).probe_failed);
 assert(!paused.process(1,0,cache("probe",20),false).probe_ready);
 assert(!paused.process(1,0,chat("~g1:forged"),false).allowed);
 paused.process(1,0,cache("tick",9),true);
 assert(paused.process(1,0,cache("probe",20),true).probe_ready);
 assert(paused.process(1,0,cache("tick",16),true).downlink==chat("~g1:20:1:16:1",832,832));
 for(unsigned i=0;i<100;++i)assert(paused.process(1,0,cache("tick",16),true).downlink.empty());
 // Retiring a publisher blocks both later ticks and repeat protocol handshakes.
 paused.depart(1);
 assert(paused.process(1,0,meta,true).downlink.empty());
 assert(paused.process(1,0,cache("tick",40),true).downlink.empty());
 Session fresh;fresh.configure(4,832,832,21);fresh.process(1,0,meta,true);
 assert(fresh.process(1,0,cache("tick",8),true).probe_packet);
 fresh.process(1,0,cache("tick",9),true);
 assert(!fresh.process(1,0,cache("probe",20),true).probe_ready);
 assert(fresh.process(1,0,cache("probe",21),true).probe_ready);
 assert(fresh.process(1,0,cache("tick",16),true).downlink==chat("~g1:21:1:16:1",832,832));
 // Deterministic malformed-byte fuzz, no process state mutation on rejected batch.
 uint32_t seed=123;for(unsigned k=0;k<50000;++k){Bytes v(k%90);for(size_t i=0;i<v.size();++i){seed=1664525*seed+1013904223;v[i]=seed>>24;}std::vector<Event> e;decode(v,e);}
 std::cout<<"G1 action boundary, ownership, isolation, expiry, rejection and fuzz checks PASS\n";
}
