# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

from asyncio import Lock
from collections import namedtuple, OrderedDict
from enum import Enum
import random

from autobahn.asyncio.websocket import WebSocketServerProtocol, WebSocketServerFactory

class ClientState(Enum):
    justConnected = 1 # when the player first connects
    receivingWords = 2 # host is telling the server which words are in the game
    gettingName = 3 # newly joined player is telling the server his/her name
    playing = 4

lobbies = {}
lobbies_lock = Lock()

def validateName(name):
    """ Returns true if this name is valid, otherwise false."""
    return len(name) <= 10 and name.isalnum()

class SLServerProtocol(WebSocketServerProtocol):
    def onConnect(self, request):
        print("Client connecting: {0}".format(request.peer))

    def onOpen(self):
        print("Websocket connection open.")
        self.clientState = ClientState.justConnected
        self.lobby = None

    def announceMsg(self, msg, sendToSelf=True):
        for p, s in self.lobby.players.items():
            if s and (p != self.name or sendToSelf):
                s.sendMessage(msg.encode("utf8"))

    def check_giveup(self):
        """ Checks if this game has been given up, and sends the :allgiveup
            message if it has.  Also returns true if the game has been given
            up.
        """
        lenplayers = len([p for p in
                          self.lobby.players.values() if p])
        print(lenplayers)
        print(len(self.lobby.giveups))
        if len(self.lobby.giveups) >= lenplayers:
            self.announceMsg(":allgiveup")
            return True
        return False


    def onMessage(self, payload, isBinary):
        if not isBinary:
            msg = payload.decode("utf8")
            if self.clientState == ClientState.justConnected:
                smsg = msg.split(" ", 1)
                # first message will either be in the form of ":host <playername>"
                # or the form of ":join <lobbyname>"
                if smsg[0] == ":host":
                    try:
                        # name validation
                        self.name = smsg[1]
                        if not validateName(self.name):
                            raise ValueError
                        # generate lobby name
                        self.lobbyname = "0"
                        with (yield from lobbies_lock):
                            while True:
                                self.lobbyname = str(random.randrange(9999))

                                if self.lobbyname not in lobbies:
                                    break
                            Lobby = namedtuple("Lobby", ["players", "words", "giveups", "lock"])
                            self.lobby = Lobby(OrderedDict(), {}, set(), Lock())
                            lobbies[self.lobbyname] = self.lobby
                        self.clientState = ClientState.receivingWords

                    except ValueError:
                        self.sendMessage(":badname".encode("utf8"), isBinary=False)
                        self.sendClose()
                elif smsg[0] == ":join":
                    try:
                        self.lobbyname = smsg[1]
                        with (yield from lobbies_lock):
                            if self.lobbyname in lobbies:
                                self.lobby = lobbies[self.lobbyname]
                                with (yield from self.lobby.lock):
                                    # we can only join a lobby if it already has players in it
                                    if self.lobby.players:
                                        # inform the player of who is already in the game, so they
                                        # know what names have already been taken
                                        # a "y" next to a name means the player is currently in the
                                        # game-- an "n" means the player joined but later left.
                                        for p, s in self.lobby.players.items():
                                            inGame = "y" if s else "n"
                                            self.sendMessage(":player {0} {1}".format(inGame, p).encode("utf8"))
                                        self.sendMessage(":endplayers".encode("utf8"))
                                        self.clientState = ClientState.gettingName
                                    else:
                                        raise ValueError
                            else:
                                raise ValueError
                    except ValueError:
                        self.sendMessage(":nolobby".encode("utf8"))
                        self.sendClose()

            elif self.clientState == ClientState.receivingWords:
                newword, _, completed = msg.partition(" ")
                with (yield from self.lobby.lock):
                    if newword == ":endwords":
                        self.lobby.players[self.name] = self

                        self.sendMessage(self.lobbyname.encode("utf8"), isBinary=False)
                        self.clientState = ClientState.playing
                    else:
                        self.lobby.words[newword] = self.name if completed == "y" else None

            elif self.clientState == ClientState.gettingName:
                self.name = msg
                with (yield from self.lobby.lock):
                    if not validateName(self.name) or self.lobby.players.get(self.name, False):
                        self.sendMessage(":badname".encode("utf8"), isBinary=False)
                    else:
                        correct_words = []
                        # send a list of all of the words in this game
                        for word, answerer in self.lobby.words.items():
                            self.sendMessage(word.encode("utf8"))
                            if answerer:
                                correct_words.append((word, answerer))
                        self.sendMessage(":endwords".encode("utf8"))
                        for word, answerer in correct_words:
                            self.sendMessage(":attempt {0} {1}".format(
                                word, answerer).encode("utf8"))

                        # also show who has voted to give up
                        for giveupper in self.lobby.giveups:
                            self.sendMessage((":giveup "+giveupper).encode("utf8"))

                        if (self.check_giveup()):
                            self.lobby.giveups.add(self.name)

                        # inform everyone else that this player has joined
                        # the game
                        self.announceMsg(":join {}".format(self.name))
                        self.lobby.players[self.name] = self
                        self.clientState = ClientState.playing

            elif self.clientState == ClientState.playing:
                smsg = msg.split(" ", 1)
                try:
                    if smsg[0] == ":attempt":
                        word = smsg[1]
                        with (yield from self.lobby.lock):
                            try:
                                if not self.lobby.words[word]:
                                    self.lobby.words[word] = self.name
                            except KeyError:
                                pass
                            self.announceMsg(":attempt {0} {1}".format(
                                word, self.name), sendToSelf=False)
                    elif smsg[0] == ":giveup":
                        with (yield from self.lobby.lock):
                            self.lobby.giveups.add(self.name)
                            # send the give up message to everyone in the game
                            self.announceMsg(":giveup "+self.name, sendToSelf=False)
                            self.check_giveup()
                    elif smsg[0] == ":ungiveup":
                        with (yield from self.lobby.lock):
                            self.lobby.giveups.discard(self.name)
                            self.announceMsg(":ungiveup "+self.name, sendToSelf=False)
                except (ValueError, AttributeError):
                    pass

    def onClose(self, wasClean, code, reason):
        # inform everyone else that this player has left the game
        if self.clientState == ClientState.playing:
            with (yield from self.lobby.lock):
                self.lobby.players[self.name] = None
                self.lobby.giveups.discard(self.name)

                if any(self.lobby.players.values()):
                    self.announceMsg(":quit "+self.name)
                    self.check_giveup()
                else:
                    # if there is no one left in this game, delete it
                    with (yield from lobbies_lock):
                        print("Deleting game "+self.lobbyname)
                        del lobbies[self.lobbyname]
        print("Websocket connection closed: {0}".format(reason))

if __name__ == "__main__":
    import asyncio

    factory = WebSocketServerFactory()
    factory.protocol = SLServerProtocol

    loop = asyncio.get_event_loop()
    coro = loop.create_server(factory, "0.0.0.0", 443)
    server = loop.run_until_complete(coro)

    try:
        loop.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
        loop.close()
