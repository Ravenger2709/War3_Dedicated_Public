// Synthetic test harness: actual CGame handlers, native sockets and W3GS parser.
// Test-only timer aging/state perturbations are never part of the vendor patch.
#include "ghost.h"
#include "config.h"
#include "game_base.h"
#include "game.h"
#include "gameprotocol.h"
#include "gameplayer.h"
#include "map.h"
#include "util.h"
#include "native_sha256.h"
#include <fcntl.h>
#include <unistd.h>
#include <sys/resource.h>
#include <signal.h>
#include <iostream>
extern CGHost *gGHost;

// Inspect both the synthetic driver game and real autohost-created CGame.
// NativeComposition appends eligible PIDs after the canonical nine-byte rows.
static string TestHostGameJSON(CBaseGame *game)
{
    if(!game) return "null";
    BYTEARRAY slots=game->NativeComposition();
    slots.resize(slots.size()-game->NativeEligiblePlayers().size());
    return "{"+game->NativeStateJSON()+",\"ai_owned\":"+UTIL_ToString(game->NativeOwnedAIPlayers())+
           ",\"game_loaded\":"+(game->GetGameLoaded()?"true":"false")+
           ",\"lagging\":"+(game->GetLagging()?"true":"false")+
           ",\"slots_hex\":\""+UTIL_ByteArrayToHexString(slots)+"\"}";
}

class NativeGame : public CGame
{
    bool m_TestScored;
public:
    NativeGame(CGHost *host, uint16_t port=16113) : CGame(host, host->m_Map, NULL, port, GAME_PUBLIC, "Synthetic lobby", "Owner", "Owner", ""), m_TestScored(false) {}
    uint32_t key() const { return m_EntryKey; }
    string snapshot()
    {
        // Values and derived behavior from the actual game, not host defaults.
        const NativeGameplaySettings &s=m_NativeGameplay;
        return "["+UTIL_ToString((uint32_t)s.Phase3ConfigValid)+","+UTIL_ToString(s.MaxPlayers)+","+
            UTIL_ToString(s.DefaultAutoStartPlayers)+","+UTIL_ToString((uint32_t)s.VoteEnabled)+","+
            UTIL_ToString(s.VoteRequiredPercent)+","+UTIL_ToString(s.VoteMinPlayers)+","+
            UTIL_ToString(s.VoteTimeout)+","+UTIL_ToString(s.VoteCooldown)+","+
            UTIL_ToString((uint32_t)s.LobbyRefreshEnabled)+","+UTIL_ToString(s.LobbyRefreshInterval)+","+
            UTIL_ToString((uint32_t)s.LobbyAISupported)+","+UTIL_ToString(s.LobbyAITarget)+","+
            UTIL_ToString(s.LobbyAISkill)+","+UTIL_ToString(s.LobbyMinHumans)+","+
            UTIL_ToString((uint32_t)s.AutoStartEnabled)+","+UTIL_ToString(s.CountDownSeconds)+","+
            UTIL_ToString(NativeMaxPlayers())+","+UTIL_ToString(NativeAutoStartPlayers())+","+
            UTIL_ToString((uint32_t)NativeAIEnabled())+"]";
    }
    bool Update(void *fd, void *send_fd)
    {
        bool exiting = CGame::Update(fd, send_fd);
        if(exiting)
        {
            BYTEARRAY slots;
            for(size_t i=0;i<m_Slots.size();++i) UTIL_AppendByteArray(slots,m_Slots[i].GetByteArray());
            std::cout << "TEST EXIT {\"refresh_pending\":" << (m_NativeRefreshSID!=255?"true":"false")
                      << ",\"host_counter\":" << (m_HostCounter&0x0fffffff)
                      << ",\"players_total\":" << m_Players.size()
                      << ",\"computer_players\":" << NativeComputerPlayers()
                      << ",\"ai_owned\":" << NativeOwnedAIPlayers()
                      << ",\"slots_hex\":\"" << UTIL_ByteArrayToHexString(slots) << "\"}" << std::endl;
        }
        return exiting;
    }
    void EventPlayerJoined(CPotentialPlayer *potential, CIncomingJoinPlayer *joinPlayer)
    {
        // Drive the actual scored-join callback without requiring a score DB.
        if(m_TestScored) EventPlayerJoinedWithScore(potential, joinPlayer, 1000.0);
        else CBaseGame::EventPlayerJoined(potential, joinPlayer);
    }
    void test(const string &command, const string &name)
    {
        CGamePlayer *player = GetPlayerFromName(name, false);
        if(command == "mapcheck")
        {
            bool isolated = m_Map && m_Map != m_GHost->m_Map && m_Map != m_GHost->m_AutoHostMap &&
                m_Map->GetMapData() != m_GHost->m_AutoHostMap->GetMapData();
            if(isolated)
            {
                string original = *m_GHost->m_AutoHostMap->GetMapData();
                m_Map->GetMapData()->append("test-only");
                isolated = *m_GHost->m_AutoHostMap->GetMapData() == original;
                m_Map->GetMapData()->erase(m_Map->GetMapData()->size()-9);
            }
            std::cout << "TEST MAP ISOLATED " << (isolated ? 1 : 0) << std::endl;
        }
        else if(command == "slots") SendAllSlotInfo();
        else if(command == "ping") SendAll(m_Protocol->SEND_W3GS_PING_FROM_HOST());
        else if(command == "agevote") m_VoteStartedTicks -= m_GHost->m_VoteTimeout * 1000;
        else if(command == "agecooldown") m_VoteCooldownTicks -= m_GHost->m_VoteCooldown * 1000;
        // Never manufacture a gameover timer: age only a timer armed by CGame.
        else if(command == "agegameover" && m_GameOverTime) m_GameOverTime -= 61;
        else if(command == "agerefresh") { m_LastLobbyRefreshTicks -= m_GHost->m_LobbyRefreshInterval * 1000; m_GHost->m_NativeLastRefreshMS = 0; m_GHost->m_NativeRefreshStartedMS = 0; }
        else if(command == "auto") { StartCountDownAuto(m_GHost->m_RequireSpoofChecks); }
        else if(command == "unlimit") { m_PublicCommandTicks.clear(); }
        else if(command == "teamsame") { for(size_t i=0;i<m_Slots.size();++i) if(m_Slots[i].GetSlotStatus()==SLOTSTATUS_OCCUPIED) m_Slots[i].SetTeam(0); SendAllSlotInfo(); }
        else if(command == "duplicatecolour") { if(m_Slots.size()>1) m_Slots[1].SetColour(m_Slots[0].GetColour()); SendAllSlotInfo(); }
        else if(command == "spectator" && player) { unsigned char sid=GetSIDFromPID(player->GetPID()); m_Slots[sid].SetTeam(12); m_Slots[sid].SetColour(12); SendAllSlotInfo(); }
        else if(command == "hold") AddToReserved(name);
        else if(command == "freecooldown") { m_GHost->m_NativeLastRefreshMS = 0; m_GHost->m_NativeRefreshStartedMS = 0; }
        else if(command == "refresh") NativeRefresh();
        else if(command == "pendingclose") CloseSlot(m_NativeRefreshSID,false);
        else if(command == "pendingcomputer") ComputerSlot(m_NativeRefreshSID,2,false);
        else if(command == "pendingopen") OpenSlot(m_NativeRefreshSID,false);
        else if(command == "pendingcolour") ColourSlot(m_NativeRefreshSID,11);
        else if(command == "pendingswap")
        {
            for(size_t i=0;i<m_Slots.size();++i)
                if(m_Slots[i].GetSlotStatus()==SLOTSTATUS_OPEN && !m_Slots[i].GetPID())
                { SwapSlots(m_NativeRefreshSID,i); break; }
        }
        else if(command == "closeall") CloseAllSlots();
        else if(command == "openall") OpenAllSlots();
        else if(command == "load") EventGameStarted();
        else if(command == "setexit") SetExiting(true);
        else if(command == "pendingtamper" && m_NativeRefreshSID < m_Slots.size())
        {
            // Defensive restoration check: emulate a future missed mutation.
            // Never allow a stale saved slot to overwrite an occupied row.
            m_Slots[m_NativeRefreshSID].SetPID(222);
            m_Slots[m_NativeRefreshSID].SetSlotStatus(SLOTSTATUS_OCCUPIED);
            m_Slots[m_NativeRefreshSID].SetDownloadStatus(73);
            SendAllSlotInfo();
        }
        else if(command == "closeremaining") { for(size_t i=0;i<m_Slots.size();++i) if(m_Slots[i].GetSlotStatus()==SLOTSTATUS_OPEN) CloseSlot(i,false); }
        else if(command == "computer") { for(size_t i=0;i<m_Slots.size();++i) if(m_Slots[i].GetSlotStatus()==SLOTSTATUS_OPEN) { ComputerSlot(i,1,false); break; } }
        else if(command == "orphan") { for(size_t i=0;i<m_Slots.size();++i) if(m_Slots[i].GetSlotStatus()==SLOTSTATUS_OPEN) { m_Slots[i].SetPID(222); m_Slots[i].SetDownloadStatus(100); m_Slots[i].SetSlotStatus(SLOTSTATUS_OCCUPIED); break; } }
        else if(command == "scored") { m_TestScored=true; m_GHost->m_MatchMakingMethod=1; m_MinimumScore=0; m_MaximumScore=2000; }
        else if(command == "rejectscore") { m_TestScored=true; m_GHost->m_MatchMakingMethod=1; m_MinimumScore=2000; m_MaximumScore=3000; }
        else if(command == "ownerless") { m_OwnerName.clear(); ReconcileNativeAI(); }
        else if(command == "airelease")
        {
            while(!m_NativeAISlots.empty()) ReleaseNativeAI(m_NativeAISlots.begin()->first);
        }
        else if(command == "aiclose" && !m_NativeAISlots.empty()) CloseSlot(m_NativeAISlots.begin()->first,false);
        else if(command == "aiswapself" && !m_NativeAISlots.empty()) SwapSlots(m_NativeAISlots.begin()->first,m_NativeAISlots.begin()->first);
        else if(command == "aiswapinvalid" && !m_NativeAISlots.empty()) SwapSlots(m_NativeAISlots.begin()->first,99);
        else if(command == "aicolournoop" && !m_NativeAISlots.empty()) { unsigned char sid=m_NativeAISlots.begin()->first; ColourSlot(sid,m_Slots[sid].GetColour()); }
        else if(command == "aicomputerinvalid" && !m_NativeAISlots.empty()) ComputerSlot(m_NativeAISlots.begin()->first,9,false);
        else if(command == "aicomputernoop" && !m_NativeAISlots.empty())
        {
            unsigned char sid=m_NativeAISlots.begin()->first;
            for(map<unsigned char,pair<CGameSlot,CGameSlot> >::iterator i=m_NativeAISlots.begin();i!=m_NativeAISlots.end();++i)
                if(m_Slots[i->first].GetHandicap()!=100) { sid=i->first; break; }
            ComputerSlot(sid,m_Slots[sid].GetComputerType(),false);
        }
        else if(command == "balance") BalanceSlots();
        else if(command == "aisameteam")
        {
            set<unsigned char> humans=NativeEligiblePlayers();
            unsigned char sid=humans.empty()?255:GetSIDFromPID(*humans.begin());
            if(sid<m_Slots.size()) for(size_t i=0;i<m_Slots.size();++i)
                if(m_Slots[i].GetComputer()) m_Slots[i].SetTeam(m_Slots[sid].GetTeam());
            SendAllSlotInfo();
        }
        else if(command == "airestoreteams")
        {
            // Undo only the test's team perturbation on an otherwise exact
            // managed computer. Do not create computers or edit ownership.
            for(map<unsigned char,pair<CGameSlot,CGameSlot> >::iterator i=m_NativeAISlots.begin();i!=m_NativeAISlots.end();++i)
                if(i->first<m_Slots.size())
                {
                    CGameSlot restored=m_Slots[i->first];
                    restored.SetTeam(i->second.second.GetTeam());
                    if(restored.GetByteArray()==i->second.second.GetByteArray()) m_Slots[i->first]=restored;
                }
            SendAllSlotInfo();
        }
        else if(command == "closeone") { for(size_t i=0;i<m_Slots.size();++i) if(m_Slots[i].GetSlotStatus()==SLOTSTATUS_OPEN) { CloseSlot(i,false); break; } }
        else if(command == "jsoncase")
        {
            // Exercise CGame state serialization, including utility limits beyond
            // the lengths allowed for a normally configured map/game name.
            if(name == "valid") m_NativeMapName="\xC2\xA2\xE2\x82\xAC\xF0\x9F\x98\x80.w3x";
            else if(name == "invalid") m_NativeMapName="\xFF\x80\xC0\xAF\xED\xA0\x80\xF4\x90\x80\x80\xF5\x80\x80\x80";
            else if(name == "incomplete") m_NativeMapName="tail\xE2\x82";
            else if(name == "boundary2") m_NativeMapName=string(254,'x')+"\xC2\xA2";
            else if(name == "boundary3") m_NativeMapName=string(252,'x')+"\xE2\x82\xAC";
            else if(name == "boundary4") m_NativeMapName=string(252,'x')+"\xF0\x9F\x98\x80";
            else if(name == "boundary4fit") m_NativeMapName=string(251,'x')+"\xF0\x9F\x98\x80";
            else if(name == "escape") m_NativeMapName=string("Q\"\\\n\r\t\0",7);
        }
        else if(command == "inspect") std::cout << "TEST IDENTITY " << (player?1:0) << ' ' << (player&&player->GetSpoofed()?1:0) << std::endl;
        else if(command == "end") { m_Exiting=true; }
        else if(command == "startagain") EventGameStarted();
        m_GHost->UpdateNativeFeedback(true);
        BYTEARRAY slots;
        for(size_t i=0;i<m_Slots.size();++i) UTIL_AppendByteArray(slots,m_Slots[i].GetByteArray());
        string owned;
        for(map<unsigned char,pair<CGameSlot,CGameSlot> >::iterator i=m_NativeAISlots.begin();i!=m_NativeAISlots.end();++i)
            if(i->first<m_Slots.size() && m_Slots[i->first].GetByteArray()==i->second.second.GetByteArray())
                owned+=(owned.empty()?"":",")+UTIL_ToString((uint32_t)i->first);
        std::cout << "TEST STATE {" << NativeStateJSON() << ",\"vote_active\":" << (m_VoteActive?"true":"false") << ",\"vote_cooling\":" << (m_VoteCooling?"true":"false") << ",\"refresh_pending\":" << (m_NativeRefreshSID!=255?"true":"false") << ",\"ai_owned\":" << NativeOwnedAIPlayers() << ",\"ai_owned_sids\":[" << owned << "],\"slots_hex\":\"" << UTIL_ByteArrayToHexString(slots)
                  << "\",\"game_loaded\":" << (m_GameLoaded?"true":"false")
                  << ",\"lagging\":" << (m_Lagging?"true":"false")
                  << ",\"gameover_started\":" << (m_GameOverTime?"true":"false")
                  << ",\"gameover_age\":" << (m_GameOverTime?GetTime()-m_GameOverTime:0)
                  << ",\"refresh_sid\":" << (uint32_t)m_NativeRefreshSID
                  << ",\"refresh_remaining\":" << m_NativeRefreshWave.size()
                  << ",\"refresh_started_ms\":" << m_GHost->m_NativeRefreshStartedMS
                  << ",\"refresh_count\":" << m_GHost->m_NativeRefreshCount
                  << ",\"refresh_sequence\":" << m_GHost->m_NativeRefreshSequence
                  << ",\"game_sync_counter\":" << m_SyncCounter
                  << ",\"game_ticks\":" << m_GameTicks
                  << ",\"players_total\":" << m_Players.size() << "}" << std::endl;
    }
};
// Only this offline executable defines the friend; no test command enters GHost.
class NativeKickTestAccess
{
public:
    static void Apply(CBaseGame *game,const string &operation,uint32_t seconds)
    {
        if(!game) { std::cout << "TEST KICKSTATE null" << std::endl; return; }
        uint64_t now=NativeMonotonicMS(), age=(uint64_t)seconds*1000;
        if(seconds<=3600)
        {
            if(operation=="agekick" && game->m_NativeKickActive) game->m_NativeKickStartedMS=now>=age?now-age:0;
            if(operation=="agekickcooldown" && game->m_NativeKickCooling) game->m_NativeKickCooldownMS=now>=age?now-age:0;
        }
        if(operation=="kickunlimit") game->m_NativeKickCommandMS.clear();
        game->UpdateNativeKick();
        map<unsigned char,CGamePlayer *> humans=game->NativeKickHumans();
        std::cout << "TEST KICKSTATE {\"active\":" << (game->m_NativeKickActive?"true":"false")
                  << ",\"cooling\":" << (game->m_NativeKickCooling?"true":"false")
                  << ",\"target_pid\":" << (uint32_t)game->m_NativeKickTarget
                  << ",\"yes\":" << game->m_NativeKickYes.size()
                  << ",\"required\":" << game->m_NativeKickRequired
                  << ",\"eligible\":" << (game->m_NativeKickActive?game->m_NativeKickMembers.size()-1:0)
                  << ",\"host_counter\":" << game->GetHostCounter() << ",\"human_pids\":[";
        bool first=true;
        for(map<unsigned char,CGamePlayer *>::iterator i=humans.begin();i!=humans.end();++i)
        { std::cout << (first?"":",") << (uint32_t)i->first;first=false; }
        std::cout << "],\"voter_pids\":[";first=true;
        for(set<unsigned char>::iterator i=game->m_NativeKickYes.begin();i!=game->m_NativeKickYes.end();++i)
        { std::cout << (first?"":",") << (uint32_t)*i;first=false; }
        std::cout << "]}" << std::endl;
    }
};

int main(int argc,char **argv)
{
    if(argc!=2) return 2;
    CConfig cfg; cfg.Read(argv[1]);
    gGHost=new CGHost(&cfg);
    if(gGHost->m_Exiting) { delete gGHost; gGHost=NULL; return 78; }
    fcntl(0,F_SETFL,fcntl(0,F_GETFL)|O_NONBLOCK);
    std::cout << "TEST READY 0" << std::endl;
    string pending;
    CNativeStartupCatalog *heldCatalog=NULL;
    bool quit=false;
    uint32_t started=GetTicks();
    while(!quit && GetTicks()-started<180000)
    {
        char bytes[512]; ssize_t n=read(0,bytes,sizeof bytes);
        if(n>0) pending.append(bytes,n);
        size_t end;
        while((end=pending.find('\n'))!=string::npos)
        {
            string command=pending.substr(0,end); pending.erase(0,end+1);
            if(command=="quit") quit=true;
            else if(command.compare(0,10,"kickstate ")==0 || command.compare(0,8,"agekick ")==0 ||
                    command.compare(0,16,"agekickcooldown ")==0 || command.compare(0,12,"kickunlimit ")==0)
            {
                std::istringstream input(command); string operation; int index=-99; uint32_t seconds=0;
                input >> operation >> index >> seconds;
                CBaseGame *selected=NULL;
                if(index==-2) selected=gGHost->m_CurrentGame;
                else if(index>=0 && (size_t)index<gGHost->m_Games.size()) selected=gGHost->m_Games[index];
                else if(index==-1)
                {
                    selected=dynamic_cast<NativeGame*>(gGHost->m_CurrentGame);
                    if(!selected) for(size_t i=0;i<gGHost->m_Games.size();++i)
                        if(dynamic_cast<NativeGame*>(gGHost->m_Games[i])) { selected=gGHost->m_Games[i]; break; }
                }
                NativeKickTestAccess::Apply(selected,operation,seconds);
            }
            else if(command.compare(0, 7, "budget ")==0)
            {
                std::istringstream input(command.substr(7));
                uint32_t games=0; uint64_t size, defaultSize=0, required=0;
                std::vector<uint64_t> sizes;
                input >> games >> defaultSize;
                while(input >> size) sizes.push_back(size);
                bool valid=NativePreloadPayloadBudget(sizes, defaultSize, games, required);
                std::cout << "TEST BUDGET " << valid << ' ' << required << std::endl;
            }
            else if(command.compare(0, 10, "admission ")==0)
            {
                std::istringstream input(command.substr(10));
                uint32_t games=0; uint64_t size, defaultSize=0, adminSize=0, current=0, limit=0, required=0;
                std::vector<uint64_t> sizes;
                input >> games >> defaultSize >> adminSize >> current >> limit;
                while(input >> size) sizes.push_back(size);
                bool valid=NativePreloadAdmissionBudget(sizes, defaultSize, adminSize, games, current, limit, required);
                std::cout << "TEST ADMISSION " << valid << ' ' << required << std::endl;
            }
            else if(command=="cgroup")
            {
                uint64_t current=0, limit=0;
                bool valid=NativePreloadCgroupSnapshot(current, limit);
                std::cout << "TEST CGROUP " << valid << ' ' << current << ' ' << limit << std::endl;
            }
            else if(command.compare(0, 9, "envelope ")==0)
            {
                std::istringstream input(command.substr(9));
                int fd=-1; uint32_t games=0; uint64_t defaultSize=0, current=0, limit=0, required=0;
                input >> fd >> games >> defaultSize >> current >> limit;
                bool valid=NativePreloadManifestEnvelope(fd, games, defaultSize, current, limit, required);
                std::cout << "TEST ENVELOPE " << valid << ' ' << required << std::endl;
            }
            else if(command=="cgroupfull")
            {
                uint64_t headroom=0; bool valid=NativePreloadCgroupHeadroom(headroom);
                std::cout << "TEST CGROUPFULL " << valid << ' ' << headroom << std::endl;
            }
            else if(command.compare(0, 8, "catalog ")==0)
            {
                std::istringstream input(command.substr(8)); int fd=-1; input >> fd;
                CNativeStartupCatalog *catalog=NativeBuildStartupCatalog(gGHost,fd);
                std::cout << "TEST CATALOG " << (catalog ? catalog->Count() : 0);
                if(catalog) {
                    std::cout << " " << catalog->CycleCount();
                    for(size_t i=0;i<catalog->CycleCount();++i) std::cout << " " << catalog->CycleIndex(i);
                    for(size_t i=0;i<catalog->Count();++i) std::cout << " " << catalog->ID(i) << ":" << catalog->MapPath(i);
                }
                std::cout << std::endl; delete catalog;
            }
            else if(command.compare(0, 12, "cataloghold ")==0)
            {
                std::istringstream input(command.substr(12)); int fd=-1; input >> fd;
                if(!heldCatalog) heldCatalog=NativeBuildStartupCatalog(gGHost,fd);
                std::cout << "TEST CATALOGHOLD " << (heldCatalog ? heldCatalog->Count() : 0) << std::endl;
            }
            else if(command.compare(0, 13, "catalogstage ")==0)
            {
                std::istringstream input(command.substr(13)); string id; size_t index=9999; input >> id >> index;
                std::cout << "TEST CATALOGSTAGE " << (heldCatalog ? heldCatalog->Stage(gGHost,id,index) : 0) << std::endl;
            }
            else if(command.compare(0, 8, "closefd ")==0)
            {
                std::istringstream input(command.substr(8)); int fd=-1; input >> fd;
                std::cout << "TEST CLOSEFD " << (fd>=3 && close(fd)==0) << std::endl;
            }
#ifdef NATIVE_CGROUP_TEST
            else if(command.compare(0, 11, "cgroupfake ")==0)
            {
                std::istringstream input(command.substr(11)); string root, membership; input >> root >> membership;
                uint64_t headroom=0; bool valid=NativeTestCgroupWalk(root,membership,headroom);
                std::cout << "TEST CGROUPFAKE " << valid << ' ' << headroom << std::endl;
            }
#endif
            else if(command.compare(0, 11, "hashvector ")==0)
            {
                std::istringstream input(command.substr(11)); int fd=-1; string expected; size_t chunk=0;
                input >> fd >> expected >> chunk;
                string data; char buffer[65536]; ssize_t count=0; off_t offset=0;
                bool valid=fd>=3 && chunk>0 && chunk<=65536;
                while(valid)
                {
                    do { count=pread(fd,buffer,sizeof buffer,offset); } while(count<0 && errno==EINTR);
                    if(count<0 || data.size()+(size_t)count>2*1024*1024) { valid=false; break; }
                    if(!count) break;
                    data.append(buffer,(size_t)count); offset+=count;
                }
                NativeSHA256 hash; unsigned char bytes[32]; string actual;
                if(valid)
                {
                    valid=hash.Update(NULL,0);
                    for(size_t pos=0;valid && pos<data.size();pos+=chunk)
                        valid=hash.Update(data.data()+pos,std::min(chunk,data.size()-pos));
                    valid=valid && hash.Final(bytes);
                    static const char hex[]="0123456789abcdef";
                    if(valid) for(size_t i=0;i<32;++i) { actual+=hex[bytes[i]>>4]; actual+=hex[bytes[i]&15]; }
                }
                bool sealed=valid && !hash.Update(NULL,0) && !hash.Final(bytes);
                NativeSHA256 invalid;
                bool rejects=!invalid.Update(NULL,1) && !invalid.Final(bytes);
                std::cout << "TEST HASHVECTOR " << (valid && actual==expected) << ' '
                          << (valid && NativeMapSHA256Matches(data,expected)) << ' ' << sealed << ' ' << rejects << std::endl;
            }
            else if(command.compare(0, 13, "manifesthash ")==0)
            {
                std::istringstream input(command.substr(13)); int fd=-1; string digest; input >> fd >> digest;
                std::cout << "TEST MANIFESTHASH " << NativePreloadManifestDigestMatches(fd,digest) << std::endl;
            }
            else if(command.compare(0,13,"emptytimeout ")==0)
            {
                string value = command.substr(13);
                gGHost->m_EmptyLobbyRotationSeconds=std::min((uint32_t)86400,UTIL_ToUInt32(value));
            }
            else if(command=="generation")
                std::cout << "TEST GENERATION " << gGHost->m_LifecycleGeneration << std::endl;
            else if(command.compare(0, 17, "catalogholdbound ")==0)
            {
                std::istringstream input(command.substr(17)); int fd=-1; string digest; input >> fd >> digest;
                if(!heldCatalog) heldCatalog=NativeBuildBoundStartupCatalog(gGHost,fd,digest);
                std::cout << "TEST CATALOGBOUNDED " << (heldCatalog ? heldCatalog->Count() : 0) << std::endl;
            }
            else if(command.compare(0, 15, "catalogrequest ")==0)
            {
                std::istringstream input(command.substr(15)); string directory,id; input >> directory >> id;
                std::cout << "TEST CATALOGREQUEST " << (heldCatalog && heldCatalog->HandleRequest(gGHost,directory,id)) << std::endl;
            }
            else if(command.compare(0, 14, "catalogattach ")==0)
            {
                string directory=command.substr(14);
                std::cout << "TEST CATALOGATTACH " << (heldCatalog && heldCatalog->AttachIPC(gGHost,directory)) << std::endl;
            }
            else if(command=="catalogstate")
            {
                gGHost->m_LastNativeState.clear(); gGHost->EmitNativeState();
            }
            else if(command=="catalogpoll")
                std::cout << "TEST CATALOGPOLL " << (heldCatalog && heldCatalog->PollIPC(gGHost)) << std::endl;
            else if(command=="host")
            {
                std::cout << "TEST HOST {\"active_games\":" << gGHost->m_Games.size()
                          << ",\"max_games\":" << gGHost->m_MaxGames
                          << ",\"lobby\":" << TestHostGameJSON(gGHost->m_CurrentGame) << ",\"games\":[";
                for(size_t i=0;i<gGHost->m_Games.size();++i)
                    std::cout << (i?",":"") << TestHostGameJSON(gGHost->m_Games[i]);
                std::cout << "]}" << std::endl;
            }
            else if(command=="journalfault")
            {
                struct rlimit lim; if(getrlimit(RLIMIT_FSIZE,&lim)) return 4;
                lim.rlim_cur=0; if(signal(SIGXFSZ,SIG_IGN)==SIG_ERR || setrlimit(RLIMIT_FSIZE,&lim)) return 4;
            }
            else if(command=="journalrestore")
            {
                struct rlimit lim; if(getrlimit(RLIMIT_FSIZE,&lim)) return 4;
                lim.rlim_cur=lim.rlim_max; if(setrlimit(RLIMIT_FSIZE,&lim)) return 4;
            }
            else if(command=="enablehost") gGHost->m_Enabled=true;
            else if(command=="niceshutdown") gGHost->m_ExitingNice=true;
            else if(command=="rehostrunning" && !gGHost->m_Games.empty())
            { gGHost->m_Games[0]->SetAutoStartPlayers(6); gGHost->m_Games[0]->SetRefreshError(true); }
            else if(command=="counteroverflow") gGHost->m_HostCounter=0x10000000;
            else if(command.compare(0, 11, "mapfdstate ")==0)
            {
                std::istringstream input(command.substr(11));
                int fd=-1; string digest, path; input >> fd >> digest >> path;
                CConfig config; config.Read(path); CMap map(gGHost, &config, path, fd, digest);
                std::cout << "TEST MAPFDSTATE " << map.GetValid() << ' ' << map.GetMapLoadInGame() << ' '
                          << (unsigned)map.GetMapSpeed() << ' ' << (unsigned)map.GetMapVisibility() << ' '
                          << (unsigned)map.GetMapObservers() << ' ' << (unsigned)map.GetMapFlags() << std::endl;
            }
            else if(command.compare(0, 10, "mapfdhash ")==0)
            {
                std::istringstream input(command.substr(10));
                int fd=-1; string digest, path;
                input >> fd >> digest >> path;
                CConfig config; config.Read(path);
                CMap map(gGHost, &config, path, fd, digest);
                std::cout << "TEST MAPFDHASH " << map.GetValid() << ' '
                          << map.GetMapData()->size() << std::endl;
            }
            else if(command.compare(0, 6, "mapfd ")==0)
            {
                std::istringstream input(command.substr(6));
                int fd=-1; string path;
                input >> fd >> path;
                CConfig config; config.Read(path);
                CMap map(gGHost, &config, path, fd);
                std::cout << "TEST MAPFD " << map.GetValid() << ' '
                          << map.GetMapData()->size() << std::endl;
            }
            else if(command.compare(0, 7, "preset ")==0 || command.compare(0, 15, "presetvalidate ")==0)
            {
                bool validating=command.compare(0, 15, "presetvalidate ")==0;
                string path=command.substr(validating ? 15 : 7);
                CConfig config; config.Read(path);
                CMap map(gGHost, &config, path);
                NativeNextPreset s = {};
                s.GameName=config.GetString("test_game_name", "Next preset");
                s.MaxPlayers=config.GetInt("test_max", 4);
                s.DefaultAutoStartPlayers=config.GetInt("test_autostart", 4);
                s.AutoStartEnabled=config.GetInt("test_auto_enabled", 1);
                s.CountDownSeconds=config.GetInt("test_countdown", 2);
                s.VoteEnabled=config.GetInt("test_vote", 1);
                s.VoteRequiredPercent=config.GetInt("test_vote_percent", 60);
                s.VoteMinPlayers=config.GetInt("test_vote_min", 2);
                s.VoteTimeout=config.GetInt("test_vote_timeout", 60);
                s.VoteCooldown=config.GetInt("test_vote_cooldown", 30);
                s.LobbyRefreshEnabled=config.GetInt("test_refresh", 1);
                s.LobbyRefreshInterval=config.GetInt("test_refresh_interval", 120);
                s.LobbyAISupported=config.GetInt("test_ai", 0);
                s.LobbyAITarget=config.GetInt("test_ai_target", 0);
                s.LobbyAISkill=config.GetInt("test_ai_skill", 1);
                s.LobbyMinHumans=config.GetInt("test_min_humans", 2);
                if(validating) std::cout << "TEST PRESETVALIDATE " << gGHost->ValidateNativeNextPreset(&map,s) << std::endl;
                else std::cout << "TEST PRESET " << gGHost->ApplyNativeNextPreset(&map,s) << std::endl;
            }
            else if(command.compare(0, 9, "presetfd ")==0)
            {
                std::istringstream input(command.substr(9));
                int fd=-1; string path;
                input >> fd >> path;
                CConfig config; config.Read(path);
                CMap map(gGHost, &config, path, fd);
                NativeNextPreset s = {};
                s.GameName=config.GetString("test_game_name", "Next preset");
                s.MaxPlayers=config.GetInt("test_max", 4);
                s.DefaultAutoStartPlayers=config.GetInt("test_autostart", 4);
                s.AutoStartEnabled=config.GetInt("test_auto_enabled", 1);
                s.CountDownSeconds=config.GetInt("test_countdown", 2);
                s.VoteEnabled=config.GetInt("test_vote", 1);
                s.VoteRequiredPercent=config.GetInt("test_vote_percent", 60);
                s.VoteMinPlayers=config.GetInt("test_vote_min", 2);
                s.VoteTimeout=config.GetInt("test_vote_timeout", 60);
                s.VoteCooldown=config.GetInt("test_vote_cooldown", 30);
                s.LobbyRefreshEnabled=config.GetInt("test_refresh", 1);
                s.LobbyRefreshInterval=config.GetInt("test_refresh_interval", 120);
                s.LobbyAISupported=config.GetInt("test_ai", 0);
                s.LobbyAITarget=config.GetInt("test_ai_target", 0);
                s.LobbyAISkill=config.GetInt("test_ai_skill", 1);
                s.LobbyMinHumans=config.GetInt("test_min_humans", 2);
                std::cout << "TEST PRESETFD " << gGHost->ApplyNativeNextPreset(&map,s) << std::endl;
            }
            else if(command=="autohost") gGHost->m_LastAutoHostTime-=31;
            else if(command=="startlobby" && gGHost->m_CurrentGame) gGHost->m_CurrentGame->EventGameStarted();
            else if(command=="trycreate") gGHost->CreateGame(gGHost->m_AutoHostMap, GAME_PUBLIC, false, "Next lobby", "Owner", "Owner", "", false);
            else if(command=="catalogcheck" || command=="catalogcheckbad" || command=="catalogcheckdigest" ||
                    command=="catalogcheckindex" || command=="catalogchecksettings" || command=="catalogcheckgeneration" ||
                    command=="catalogcheckmap" || command=="catalogattempt" || command=="catalogattemptbad")
            {
                int result=-9;
                if(gGHost->m_NativeStartupCatalog && gGHost->m_NativePendingMap && gGHost->m_NativePendingPreset)
                {
                    string saved=gGHost->m_NativePendingCatalogID, digest=gGHost->m_NativePendingCatalogDigest;
                    string generation=gGHost->m_LifecycleGeneration;
                    uint32_t index=gGHost->m_NativePendingCatalogIndex, maximum=gGHost->m_NativePendingPreset->MaxPlayers;
                    if(command=="catalogcheckbad" || command=="catalogattemptbad") gGHost->m_NativePendingCatalogID=string(32,'f');
                    if(command=="catalogcheckdigest") gGHost->m_NativePendingCatalogDigest=string(64,'f');
                    if(command=="catalogcheckindex") ++gGHost->m_NativePendingCatalogIndex;
                    if(command=="catalogchecksettings") ++gGHost->m_NativePendingPreset->MaxPlayers;
                    if(command=="catalogcheckgeneration") gGHost->m_LifecycleGeneration=string(32,'f');
                    CMap *candidate=command=="catalogcheckmap" ? gGHost->m_AutoHostMap : gGHost->m_NativePendingMap;
                    result=gGHost->m_NativeStartupCatalog->CheckAdmission(gGHost,candidate,gGHost->m_NativePendingPreset->GameName);
                    if(command=="catalogattempt" || command=="catalogattemptbad")
                    {
                        gGHost->m_NativeCatalogAdmission=true;
                        gGHost->CreateGame(gGHost->m_NativePendingMap,GAME_PUBLIC,false,gGHost->m_NativePendingPreset->GameName,"Owner","Owner","",false);
                        gGHost->m_NativeCatalogAdmission=false;
                    }
                    gGHost->m_LifecycleGeneration=generation;
                    gGHost->m_NativePendingCatalogID=saved;gGHost->m_NativePendingCatalogDigest=digest;
                    gGHost->m_NativePendingCatalogIndex=index;gGHost->m_NativePendingPreset->MaxPlayers=maximum;
                }
                std::cout << "TEST CATALOGADMISSION " << result << std::endl;
            }
            else if(command=="rehosterror" && gGHost->m_CurrentGame) gGHost->m_CurrentGame->SetRefreshError(true);
            else if(command=="closecurrent" && gGHost->m_CurrentGame) gGHost->m_CurrentGame->SetExiting(true);
            else if(command=="removehost" && gGHost->m_CurrentGame) { delete gGHost->m_CurrentGame; gGHost->m_CurrentGame=NULL; }
            else if(command=="create" && !gGHost->m_CurrentGame) { gGHost->m_CurrentGame=new NativeGame(gGHost); gGHost->m_CurrentGame->UpdateExternalPublication(); std::cout << "TEST KEY " << static_cast<NativeGame*>(gGHost->m_CurrentGame)->key() << std::endl; }
            else if(command=="snapshots" && dynamic_cast<NativeGame*>(gGHost->m_CurrentGame))
            {
                NativeGame *first=static_cast<NativeGame*>(gGHost->m_CurrentGame);
                string before=first->snapshot();
                gGHost->m_MaxPlayers=8; gGHost->m_DefaultAutoStartPlayers=4;
                gGHost->m_VoteEnabled=false; gGHost->m_VoteRequiredPercent=80;
                gGHost->m_VoteMinPlayers=1; gGHost->m_VoteTimeout=90; gGHost->m_VoteCooldown=45;
                gGHost->m_LobbyRefreshEnabled=false; gGHost->m_LobbyRefreshInterval=20;
                gGHost->m_LobbyAISupported=true; gGHost->m_LobbyAITarget=4;
                gGHost->m_LobbyAISkill=2; gGHost->m_LobbyMinHumans=1;
                gGHost->m_AutoStartEnabled=false; gGHost->m_CountDownSeconds=12;
                NativeGame second(gGHost,16114);
                string secondBefore=second.snapshot();
                // A third configuration catches either object retaining a host reference.
                gGHost->m_Phase3ConfigValid=false; gGHost->m_MaxPlayers=2;
                gGHost->m_DefaultAutoStartPlayers=2; gGHost->m_VoteEnabled=true;
                gGHost->m_VoteRequiredPercent=1; gGHost->m_VoteMinPlayers=2;
                gGHost->m_VoteTimeout=1; gGHost->m_VoteCooldown=1;
                gGHost->m_LobbyRefreshEnabled=true; gGHost->m_LobbyRefreshInterval=10;
                gGHost->m_LobbyAISupported=false; gGHost->m_LobbyAITarget=0;
                gGHost->m_LobbyAISkill=0; gGHost->m_LobbyMinHumans=2;
                gGHost->m_AutoStartEnabled=true; gGHost->m_CountDownSeconds=0;
                std::cout << "TEST SNAPSHOTS [" << before << ',' << first->snapshot() << ','
                          << secondBefore << ',' << second.snapshot() << "]" << std::endl;
                gGHost->m_Phase3ConfigValid=true;
            }
            else
            {
                NativeGame *game=dynamic_cast<NativeGame*>(gGHost->m_CurrentGame);
                if(!game && !gGHost->m_Games.empty()) game=dynamic_cast<NativeGame*>(gGHost->m_Games.back());
                if(game) { size_t sep=command.find(' '); game->test(command.substr(0,sep),sep==string::npos?"":command.substr(sep+1)); }
                else if(gGHost->m_CurrentGame && command=="slots") gGHost->m_CurrentGame->SendAllSlotInfo();
                else if(gGHost->m_CurrentGame && command=="ping") { CGameProtocol protocol(gGHost); gGHost->m_CurrentGame->SendAll(protocol.SEND_W3GS_PING_FROM_HOST()); }
            }
            std::cout << "TEST COMMAND " << command << std::endl;
        }
        if(!quit && gGHost->Update(10000)) break;
    }
    // Closing a CGame can enqueue SQLite player/stat callables. Exercise the
    // normal host event loop to drain them before deleting CGHost; its upstream
    // destructor intentionally leaves unfinished callables for process exit.
    // Delete the lobby directly so quit still tests pending-refresh destruction.
    gGHost->m_AutoHostMaximumGames=0;
    delete gGHost->m_CurrentGame; gGHost->m_CurrentGame=NULL;
    for(size_t i=0;i<gGHost->m_Games.size();++i) gGHost->m_Games[i]->StopPlayers("test completed");
    gGHost->m_ExitingNice=true;
    uint32_t draining=GetTicks();
    while(GetTicks()-draining<5000)
        if(gGHost->Update(10000)) break;
    bool clean=gGHost->m_Games.empty() && gGHost->m_Callables.empty();
    std::cout << "TEST DRAIN " << gGHost->m_Games.size() << ' ' << gGHost->m_Callables.size() << std::endl;
    delete heldCatalog;
    delete gGHost; gGHost=NULL; return clean?0:3;
}
